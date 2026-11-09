"""Рефлексия: после разговора бот сам решает, что из него стоит запомнить о пользователе.

Отдельный вызов LLM с узкой задачей (а не инструмент посреди диалога): модель видит весь кусок переписки и
текущую память целиком, поэтому лучше отличает устойчивые факты от разовых и не плодит дубли. Ответы
пользователю при этом не замедляются.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pim.core.models import ChatMessage, Memory, MemoryKind, MemorySource, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import to_local
from pim.llm.base import LLMProvider, Message

log = logging.getLogger(__name__)

# Сколько последних неразобранных реплик отдаём модели за раз
MAX_MESSAGES = 40
MAX_CONTENT_CHARS = 300

REFLECTION_PROMPT = """\
Ты — модуль долговременной памяти личного ассистента. Сегодня {today}.
Ниже — что ассистент уже знает о пользователе и свежий фрагмент их переписки.
Найди в переписке НОВЫЕ устойчивые факты о пользователе, которые пригодятся в будущих разговорах,
и факты из памяти, которые разговор уточнил или опроверг.

Стоит запоминать: людей и питомцев в жизни пользователя (имя, кем приходится, дни рождения), распорядок и привычки,
предпочтения, работу и учёбу, здоровье и ограничения (аллергии, диеты), важные планы на ближайшее время
(для них until_date — до какой даты факт актуален).
Реплики, начинающиеся с «[Пересланное сообщение от …]», — слова ДРУГИХ людей: факты оттуда не приписывай
пользователю (можно запомнить лишь то, что прямо касается его самого, например «Андрей — его начальник»).
НЕ запоминай: сами дела и напоминания (их ассистент ведёт отдельно), разовые события, слова ассистента,
догадки и то, чего пользователь прямо не говорил, то, что уже есть в памяти (даже другими словами).
В update — только факты, СМЫСЛ которых разговор изменил (другая дата, новое место работы, факт перестал
быть верным). Не переформулируй факты из памяти ради стиля — если смысл тот же, не трогай.
Если запоминать нечего — это нормально, верни пустые списки.

kind: person — люди и питомцы; routine — распорядок, работа, привычки; preference — предпочтения и
ограничения (еда, время, как общаться); fact — всё остальное.
Формулируй коротко, без слов «пользователь» и «я», в стиле: «Брат — Савва», «Кот — Барсик»,
«Работает тимлидом, в офисе по вт и чт», «Не ест мясо», «Встаёт около 10 утра».

Память сейчас (number. kind: content):
{memories}

Переписка:
{dialog}

Ответь ТОЛЬКО JSON, без пояснений и markdown:
{{"add": [{{"kind": "person|routine|preference|fact", "content": "факт одной короткой фразой", \
"until_date": "YYYY-MM-DD или пустая строка"}}],
 "update": [{{"number": 3, "content": "исправленная формулировка"}}]}}
"""


@dataclass
class ReflectionResult:
    added: list[Memory] = field(default_factory=list)
    updated: list[Memory] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.updated)


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def _render_dialog(messages: list[ChatMessage]) -> str:
    who = {"user": "Пользователь", "assistant": "Ассистент"}
    return "\n".join(f"{who.get(m.role, m.role)}: {m.content}" for m in messages)


def _render_memories(memories: list[Memory]) -> str:
    return "\n".join(f"{m.number}. {m.kind}: {m.content}" for m in memories) or "(пусто)"


def parse_reflection(raw: str) -> dict:
    """JSON из ответа модели; модели любят обрамлять его текстом или ```json — вырезаем фигурные скобки."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе нет JSON")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON — не объект")
    return data


class Reflector:
    def __init__(self, llm: LLMProvider, repo: Repository, max_memories: int):
        self._llm = llm
        self._repo = repo
        self._max = max_memories

    async def reflect(self, user: User) -> ReflectionResult:
        messages = await self._repo.messages_to_reflect(user.id, MAX_MESSAGES)
        if not any(m.role == "user" for m in messages):
            if messages:
                await self._repo.mark_reflected(user.id, messages[-1].created_at)
            return ReflectionResult()

        memories = await self._repo.list_memories(user.id)
        prompt = REFLECTION_PROMPT.format(
            today=to_local(utcnow(), user.timezone).date().isoformat(),
            memories=_render_memories(memories),
            dialog=_render_dialog(messages),
        )
        answer = await self._llm.chat([Message("user", prompt)], [])
        try:
            data = parse_reflection(answer.content)
        except ValueError:
            # Не повторяем бесконечно один и тот же кусок: помечаем разобранным и идём дальше
            log.warning("user %s: unparsable reflection answer: %r", user.id, answer.content[:300])
            data = {}

        result = await self._apply(user, data, memories)
        await self._repo.mark_reflected(user.id, messages[-1].created_at)
        log.info("user %s: reflection added %d, updated %d", user.id, len(result.added), len(result.updated))
        return result

    async def _apply(self, user: User, data: dict, memories: list[Memory]) -> ReflectionResult:
        result = ReflectionResult()
        known = {_norm(m.content) for m in memories}
        by_number = {m.number: m for m in memories}

        for item in data.get("update") or []:
            if not isinstance(item, dict):
                continue
            number, content = item.get("number"), str(item.get("content") or "").strip()[:MAX_CONTENT_CHARS]
            memory = by_number.get(number) if isinstance(number, int) else None
            if not memory or not content or _norm(content) == _norm(memory.content):
                continue
            updated = await self._repo.update_memory(user.id, number, content=content)
            known.add(_norm(content))
            result.updated.append(updated)

        free = self._max - len(memories)
        for item in data.get("add") or []:
            if free <= 0:
                log.info("user %s: memory is full (%d), skipping new facts", user.id, self._max)
                break
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()[:MAX_CONTENT_CHARS]
            if not content or _norm(content) in known:
                continue
            try:
                kind = MemoryKind(item.get("kind"))
            except ValueError:
                kind = MemoryKind.FACT
            expires_at = self._until(user, item.get("until_date"))
            if expires_at is False:
                continue  # временный факт, срок которого уже прошёл
            memory = await self._repo.add_memory(
                user.id, content, kind=kind, source=MemorySource.INFERRED, expires_at=expires_at or None,
            )
            known.add(_norm(content))
            result.added.append(memory)
            free -= 1
        return result

    @staticmethod
    def _until(user: User, value) -> datetime | None | bool:
        """Конец указанного дня в зоне пользователя; None — бессрочно; False — дата уже прошла."""
        if not value:
            return None
        try:
            day = date.fromisoformat(str(value))
        except ValueError:
            return None
        end = datetime.combine(day, time(23, 59, 59), tzinfo=ZoneInfo(user.timezone))
        return False if end <= utcnow() else end
