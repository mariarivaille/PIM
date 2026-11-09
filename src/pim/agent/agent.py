import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from pim.agent.commands import parse_delete_facts
from pim.agent.prompts import build_system_prompt
from pim.agent.tools import FORWARD_MARK, TOOLS, ToolContext, memory_view, run_tool, task_view
from pim.core.models import Task, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import WEEKDAYS_RU, to_local
from pim.learning.timing import TimeProfile, load_profile
from pim.llm.base import Image, LLMProvider, Message
from pim.planning.day import DayPlanner, Plan

log = logging.getLogger(__name__)

# GigaChat делает максимум один вызов инструмента за ответ — на «добавь 5 покупок» или разбор фото нужны шаги
MAX_STEPS = 12
MAX_FORWARD_CHARS = 4000
# Пометка фото в тексте сообщения: модель понимает, что к нему приложена картинка
PHOTO_MARK = "[Фото"


@dataclass
class AgentReply:
    text: str
    touched: list[Task] = field(default_factory=list)
    auto_time: set[int] = field(default_factory=set)
    lists_touched: list[int] = field(default_factory=list)
    plan: Plan | None = None


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
                 planner: DayPlanner | None = None):
        self._llm = llm
        self._planner = planner
        self._max_memories = max_memories
        self._repo = repo
        self.history = history
        self._specs = [spec for spec, _ in TOOLS.values()]
        # Сообщения одного пользователя обрабатываются строго по очереди, разные пользователи — параллельно
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def planner(self) -> DayPlanner | None:
        return self._planner

    async def handle(self, user: User, text: str) -> AgentReply:
        async with self._locks[user.id]:
            if (numbers := parse_delete_facts(text)) is not None:
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
        """Пересланное сообщение: чужие слова, из которых надо достать дела пользователя.

        Относительные даты в нём («завтра», «в пятницу») считаются от момента отправки, а не от «сейчас».
        """
        local = to_local(sent_at, user.timezone)
        header = f"{FORWARD_MARK} от {sender}, отправлено {WEEKDAYS_RU[local.weekday()]} {local:%d.%m.%Y %H:%M}]"
        if images:
            header += " " + PHOTO_MARK + "]"
        wrapped = f"{header}\n{text[:MAX_FORWARD_CHARS]}"
        async with self._locks[user.id]:
            return await self._handle(user, wrapped, reference_time=sent_at, images=images)

    async def _delete_facts(self, user: User, numbers: list[int]) -> AgentReply:
        lines = []
        for n in numbers:
            memory = await self._repo.delete_memory(user.id, n)
            lines.append(f"🗑 Забыл факт {n}: {memory.content}" if memory else f"Факта {n} нет в памяти")
        return AgentReply(text="\n".join(lines))

    async def _system_message(self, user: User, profile: TimeProfile) -> Message:
        tasks = await self._repo.list_open_tasks(user.id)
        memories = await self._repo.list_memories(user.id)
        tasks_block = "\n".join(json.dumps(task_view(t, user.timezone), ensure_ascii=False) for t in tasks)
        memories_block = "\n".join(json.dumps(memory_view(m, user.timezone), ensure_ascii=False) for m in memories)
        shared = await self._repo.shared_list_ids(user.id)
        lists_block = "\n".join(f"«{lst.name}» — неотмеченных {open_}, всего {total}"
                                + (" (общий)" if lst.id in shared else "")
                                for lst, open_, total in await self._repo.user_lists(user.id))
        people_block = "\n".join(f"- {conn.alias}" for conn, _ in await self._repo.connections(user.id))
        return Message("system", build_system_prompt(
            to_local(utcnow(), user.timezone), user.timezone, user.digest_time, tasks_block, memories_block,
            auto_memory=user.auto_memory, review_time=user.review_time, lists_block=lists_block,
            habits_block="\n".join(profile.prompt_lines()), people_block=people_block,
        ))

    async def _handle(self, user: User, text: str, reference_time: datetime | None = None,
                      images: list[Image] | None = None) -> AgentReply:
        try:
            return await self._run(user, text, reference_time, images or [])
        finally:
            if images:
                await self._llm.cleanup(images)

    async def _run(self, user: User, text: str, reference_time: datetime | None,
                   images: list[Image]) -> AgentReply:
        history = await self.history.get(user.id)
        prev_assistant = next((m.content for m in reversed(history) if m.role == "assistant"), "")
        profile = await load_profile(self._repo, user)
        ctx = ToolContext(repo=self._repo, user=user, user_text=text, prev_assistant=prev_assistant,
                          max_memories=self._max_memories, reference_time=reference_time, profile=profile,
                          planner=self._planner)
        # в историю уходит только текст (с пометкой «[Фото…]»), сама картинка — только в этот ход
        user_msg = Message("user", text, images=images)
        messages = [await self._system_message(user, profile), *history, user_msg]

        reply = ""
        for _ in range(MAX_STEPS):
            answer = await self._llm.chat(messages, self._specs)
            messages.append(answer)
            if not answer.tool_calls:
                reply = answer.content
                break
            for call in answer.tool_calls:
                result = await run_tool(ctx, call.name, call.arguments)
                log.info("user %s: tool %s(%s) -> %s", user.id, call.name, call.arguments, result)
                messages.append(
                    Message("tool", json.dumps(result, ensure_ascii=False), tool_call_id=call.id, name=call.name)
                )
        else:
            reply = "Не получилось довести дело до конца, попробуй переформулировать."

        reply = reply.strip() or "Готово."
        await self.history.add(user.id, Message("user", text), Message("assistant", reply))
        return AgentReply(text=reply, touched=ctx.touched, auto_time=ctx.auto_time, lists_touched=ctx.lists_touched,
                          plan=ctx.plan)
