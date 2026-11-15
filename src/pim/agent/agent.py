import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from pim.agent.commands import parse_delete_facts
from pim.agent.prompts import build_system_prompt
from pim.agent.tools import COMMENT_MARK, FORWARD_MARK, TOOLS, event_when, own_words, ToolContext, memory_view, run_tool, task_view
from pim.core import calendar as cal
from pim.core.models import Task, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import WEEKDAYS_RU, format_local, to_local
from pim.learning.timing import TimeProfile, load_profile
from pim.llm.base import Image, LLMProvider, Message

log = logging.getLogger(__name__)

# GigaChat делает максимум один вызов инструмента за ответ — на «добавь 5 покупок» или разбор фото нужны шаги
MAX_STEPS = 12
MAX_FORWARD_CHARS = 4000
MAX_IMAGES = 4


@dataclass
class ForwardedPart:
    text: str
    sender: str
    sent_at: datetime
    images: list[Image] = field(default_factory=list)

OFF_TOPIC_REPLY = ("С этим я не помогаю — я веду твои дела, напоминания, списки и планы. "
                   "Если из этого следует дело, скажи — запишу.")
# Блок кода или хотя бы две строки, похожие на код
_CODE_FENCE = re.compile(r"```")
_CODE_LINE = re.compile(
    r"^\s*(def |class |for .+ in .+:|while .+:|if .+:\s*$|import |from \w+ import |return |print\(|"
    r"function |const |let |var |public |private |#include|for \(|\w+\s*=\s*\[|\}\s*$)",
    re.MULTILINE,
)


# Отказ «С этим я не помогаю», хотя пользователь говорит о деле: Gemini путает поручения и отчёты с посторонними
# просьбами («Милому надо подать на гражданство», «сделала банк»). Тогда переспрашиваем модель один раз.
_REFUSAL = re.compile(r"с этим я не помогаю", re.IGNORECASE)
_TASKISH = re.compile(
    r"(?<!\w)(?:надо|нужно|необходимо|должн\w*|напомн\w*|запиши|записать|поставь|перенеси|отмени|удали|"
    r"сделал\w*|готово|купил\w*|забрал\w*|позвонил\w*|поручи|дело|дела|задач\w*|список|списк\w*|запомни|"
    r"найди|найти|узнай|поищи|во сколько|где|сколько стоит|рецепт\w*|работает|погод\w*|"
    r"поздрав\w*|письм\w*|сообщени\w*|черновик\w*|заявлени\w*|составь|напиши)(?!\w)",
    re.IGNORECASE,
)
_NOT_OFF_TOPIC = ("Это не посторонняя просьба: пользователь говорит о своём деле или о деле близкого "
                  "(записать, поручить, закрыть, перенести, напомнить), просит найти информацию "
                  "(тогда — web_search) или написать текст от его имени (тогда — show_draft). "
                  "Выполни это инструментами.")


_CODE_REQUEST = re.compile(r"код|функци|скрипт|программ|python|питон|sql|алгоритм|сочинени|эссе|реферат|переведи",
                           re.IGNORECASE)


_RELAYED = ("Ты пересказал пользователю служебную подсказку инструмента — она была для тебя. Не упоминай её: "
            "исправь вызов инструмента, если нужно, и ответь пользователю по сути, как будто подсказки не было.")


def _shingles(text: str, n: int = 4) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.casefold())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def relays_hint(reply: str, errors: list[str]) -> bool:
    """Ответ пересказывает подсказку из {"error": ...}: Gemini так делает («Это похоже на запись… Повтори…»)."""
    said = _shingles(reply)
    return any(len(said & _shingles(err)) >= 2 for err in errors)


def wrongly_refused(reply: str, user_words: str) -> bool:
    if _CODE_REQUEST.search(user_words):
        return False  # «напиши код / сочинение» — отказ правильный
    return bool(_REFUSAL.search(reply)) and bool(_TASKISH.search(user_words))


# Факты памяти о том, где живёт пользователь: Gemini не находит город в общем блоке памяти и переспрашивает
_LOCATION = re.compile(r"жив[уеё]|город|переех|район|адрес|дом\w* на|рядом с домом", re.IGNORECASE)


def location_hint(memories) -> str:
    return "; ".join(f"«{m.content}»" for m in memories if _LOCATION.search(m.content))[:300]


def looks_like_code(text: str) -> bool:
    return bool(_CODE_FENCE.search(text)) or len(_CODE_LINE.findall(text)) >= 2
# Черновик в истории диалога: его текст написал ассистент (рефлексия не берёт оттуда фактов)
DRAFT_MARK = "[Черновик, показан пользователю]"
# Пометка фото в тексте сообщения: модель понимает, что к нему приложена картинка
PHOTO_MARK = "[Фото"


@dataclass
class AgentReply:
    text: str
    touched: list[Task] = field(default_factory=list)
    auto_time: set[int] = field(default_factory=set)
    lists_touched: list[int] = field(default_factory=list)
    drafts: list[str] = field(default_factory=list)
    events_touched: list = field(default_factory=list)


class History:
    """История диалога в БД (только текст user/assistant).

    В контекст модели идут последние `size` реплик. Протухает: если пользователь молчал дольше ttl,
    следующий разговор начинается с чистого листа, чтобы старый контекст не путал модель.
    Сами реплики при этом остаются в БД.
    """

    def __init__(self, repo: Repository, size: int, ttl_seconds: float = 3600,
                 clock: Callable[[], datetime] = utcnow):
        self._repo = repo
        self._size = size
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock

    async def get(self, user_id: int) -> list[Message]:
        items = await self._repo.recent_messages(user_id, self._size)
        if not items or self._clock() - items[-1].created_at > self._ttl:
            return []
        return [Message(m.role, m.content) for m in items]

    async def add(self, user_id: int, *messages: Message) -> None:
        await self._repo.add_messages(user_id, *((m.role, m.content) for m in messages), at=self._clock())

    async def clear(self, user_id: int) -> None:
        await self._repo.reset_context(user_id, self._clock())


class Agent:
    def __init__(self, llm: LLMProvider, repo: Repository, history: History, max_memories: int = 150,
                 searcher=None, searches_per_turn: int = 3):
        self._llm = llm
        self._searcher = searcher
        self._searches_per_turn = searches_per_turn
        self._max_memories = max_memories
        self._repo = repo
        self.history = history
        self._specs = [spec for spec, _ in TOOLS.values()]
        # Сообщения одного пользователя обрабатываются строго по очереди, разные пользователи — параллельно
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle(self, user: User, text: str) -> AgentReply:
        async with self._locks[user.id]:
            if (numbers := parse_delete_facts(own_words(text))) is not None:
                reply = await self._delete_facts(user, numbers)
                await self.history.add(user.id, Message("user", text), Message("assistant", reply.text))
                return reply
            return await self._handle(user, text)

    async def handle_photo(self, user: User, images: list[Image], caption: str = "") -> AgentReply:
        """Фото (афиша, письмо, скриншот…): найти на нём дела. Подпись — слова самого пользователя."""
        text = f"{PHOTO_MARK} с подписью] {caption}" if caption else f"{PHOTO_MARK} без подписи]"
        async with self._locks[user.id]:
            return await self._handle(user, text, images=images)

    async def handle_forwarded(self, user: User, text: str, sender: str, sent_at: datetime,
                               images: list[Image] | None = None) -> AgentReply:
        return await self.handle_forwarded_batch(user, [ForwardedPart(text, sender, sent_at, images or [])])

    async def handle_forwarded_batch(self, user: User, parts: list[ForwardedPart], comment: str = "") -> AgentReply:
        """Пересланное (одно или пачкой) и подпись пользователя к нему — одним ходом.

        Чужие слова, из которых надо достать дела пользователя; подпись говорит, что с ними сделать.
        Относительные даты («завтра», «в пятницу») считаются от момента отправки, а не от «сейчас».
        """
        blocks, images = [], []
        for part in parts:
            local = to_local(part.sent_at, user.timezone)
            header = (f"{FORWARD_MARK} от {part.sender}, отправлено "
                      f"{WEEKDAYS_RU[local.weekday()]} {local:%d.%m.%Y %H:%M}]")
            if part.images:
                header += " " + PHOTO_MARK + "]"
            blocks.append(f"{header}\n{part.text[:MAX_FORWARD_CHARS]}")
            images += part.images
        if comment.strip():
            blocks.append(f"{COMMENT_MARK} {comment.strip()}")
        reference = min(p.sent_at for p in parts)
        async with self._locks[user.id]:
            return await self._handle(user, "\n\n".join(blocks), reference_time=reference,
                                      images=images[:MAX_IMAGES] or None)

    async def handle_adjustment(self, user: User, task: Task, text: str) -> AgentReply:
        """После «✏️ Скорректировать»: слова пользователя меняют именно это поручение."""
        due = format_local(task.due_at, user.timezone) if task.due_at else "без срока"
        wrapped = (f"[Корректировка поручения: дело task_id={task.id} «{task.title}», сейчас {due}. Измени его через "
                   f"update_task(task_id={task.id}) так, как просит пользователь; новых дел не создавай]\n{text}")
        async with self._locks[user.id]:
            return await self._handle(user, wrapped, adjust_task=task)

    async def _delete_facts(self, user: User, numbers: list[int]) -> AgentReply:
        lines = []
        for n in numbers:
            memory = await self._repo.delete_memory(user.id, n)
            lines.append(f"🗑 Забыл факт {n}: {memory.content}" if memory else f"Факта {n} нет в памяти")
        return AgentReply(text="\n".join(lines))

    async def _system_message(self, user: User, profile: TimeProfile) -> Message:
        tasks = await self._repo.list_open_tasks(user.id)
        memories = await self._repo.list_memories(user.id)
        # n — номер дела в списке, который видит пользователь («Мои задачи», /tasks): «второе сделано»
        tasks_block = "\n".join(json.dumps({"n": i, **task_view(t, user.timezone)}, ensure_ascii=False)
                                for i, t in enumerate(tasks, 1))
        memories_block = "\n".join(json.dumps(memory_view(m, user.timezone), ensure_ascii=False) for m in memories)
        shared = await self._repo.shared_list_ids(user.id)
        lists_block = "\n".join(f"«{lst.name}» — неотмеченных {open_}, всего {total}"
                                + (" (общий)" if lst.id in shared else "")
                                for lst, open_, total in await self._repo.user_lists(user.id))
        people_block = "\n".join(
            f"- {conn.alias}" + (f" (ещё зовёт: {', '.join(conn.nickname_list)})" if conn.nickname_list else "")
            for conn, _ in await self._repo.connections(user.id))
        return Message("system", build_system_prompt(
            to_local(utcnow(), user.timezone), user.timezone, user.digest_time, tasks_block, memories_block,
            auto_memory=user.auto_memory, review_time=user.review_time, lists_block=lists_block,
            habits_block="\n".join(profile.prompt_lines()), people_block=people_block,
            location=location_hint(memories), calendar_block=await self._calendar_block(user),
        ))

    async def _calendar_block(self, user: User) -> str:
        """Календарь на 7 дней вперёд — чтобы модель знала занятость и отвечала «что у меня в четверг»."""
        day_start = to_local(utcnow(), user.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        occ = cal.expand(await self._repo.list_events(user.id), day_start, day_start + timedelta(days=7),
                         user.timezone)
        return "\n".join(f"id {o.event.id}: {event_when(o, user.timezone)}"
                         + (f" ({o.event.location})" if o.event.location else "")
                         + (" [повтор]" if o.event.rrule else "") for o in occ)

    async def _handle(self, user: User, text: str, reference_time: datetime | None = None,
                      images: list[Image] | None = None, adjust_task: Task | None = None) -> AgentReply:
        try:
            return await self._run(user, text, reference_time, images or [], adjust_task)
        finally:
            if images:
                await self._llm.cleanup(images)

    async def _run(self, user: User, text: str, reference_time: datetime | None,
                   images: list[Image], adjust_task: Task | None = None) -> AgentReply:
        history = await self.history.get(user.id)
        prev_assistant = next((m.content for m in reversed(history) if m.role == "assistant"), "")
        profile = await load_profile(self._repo, user)
        ctx = ToolContext(repo=self._repo, user=user, user_text=own_words(text), message_text=text,
                          prev_assistant=prev_assistant,
                          max_memories=self._max_memories, reference_time=reference_time, profile=profile,
                          adjust_task=adjust_task,
                          searcher=self._searcher, searches_left=self._searches_per_turn)
        # в историю уходит только текст (с пометкой «[Фото…]»), сама картинка — только в этот ход
        user_msg = Message("user", text, images=images)
        messages = [await self._system_message(user, profile), *history, user_msg]

        reply = ""
        retried = relay_retried = False
        errors: list[str] = []
        for _ in range(MAX_STEPS):
            answer = await self._llm.chat(messages, self._specs)
            messages.append(answer)
            if not answer.tool_calls:
                reply = answer.content
                if not retried and wrongly_refused(reply, ctx.user_text):
                    retried = True
                    log.info("user %s: refusal on a task-like message, asking again", user.id)
                    messages.append(Message("user", _NOT_OFF_TOPIC))
                    continue
                if not relay_retried and relays_hint(reply, errors):
                    relay_retried = True
                    log.info("user %s: reply relays a tool hint, asking again", user.id)
                    messages.append(Message("user", _RELAYED))
                    continue
                break
            for call in answer.tool_calls:
                result = await run_tool(ctx, call.name, call.arguments)
                if isinstance(result.get("error"), str):
                    errors.append(result["error"])
                log.info("user %s: tool %s(%s) -> %s", user.id, call.name, call.arguments, result)
                messages.append(
                    Message("tool", json.dumps(result, ensure_ascii=False), tool_call_id=call.id, name=call.name)
                )
        else:
            reply = "Не получилось довести дело до конца, попробуй переформулировать."

        reply = reply.strip()
        if DRAFT_MARK in reply:
            # правку черновика («сделай короче») модель иногда пишет прямо в ответ, скопировав пометку из истории
            before, _, draft = reply.partition(DRAFT_MARK)
            if draft.strip():
                ctx.drafts.append(draft.strip())
            reply = before.strip() or "Вот новый вариант. Поправить ещё что-нибудь?"
        reply = reply or "Готово."
        if looks_like_code(reply):
            # код Pim не пишет ни при каких условиях — промпту модель следует не всегда
            log.info("user %s: replaced off-topic code reply", user.id)
            reply = OFF_TOPIC_REPLY
        # черновик — в историю, чтобы «сделай короче» правило именно его
        shown = "".join(f"\n\n{DRAFT_MARK}\n{d}" for d in ctx.drafts)
        await self.history.add(user.id, Message("user", text), Message("assistant", reply + shown))
        return AgentReply(text=reply, touched=ctx.touched, auto_time=ctx.auto_time, lists_touched=ctx.lists_touched,
                          drafts=ctx.drafts, events_touched=ctx.events_touched)
