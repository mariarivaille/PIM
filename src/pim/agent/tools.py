"""Инструменты агента. Каждый инструмент = ToolSpec для LLM + async-обработчик.

Чтобы добавить новое умение (календарь, почта, ...) — допиши спеку и обработчик в TOOLS.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from datetime import timezone as dt_timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from pim.core.models import Memory, MemoryKind, MemorySource, Task, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import (
    DAY_KEYWORDS,
    DEFAULT_TIME,
    format_local,
    humanize_until,
    nearest_reasonable_time,
    parse_local,
    resolve_day_time,
    to_local,
)
from pim.llm.base import ToolSpec
from pim.reminders.digest import parse_hhmm

log = logging.getLogger(__name__)

END_OF_DAY = dt_time(23, 59, 59)


@dataclass
class ToolContext:
    repo: Repository
    user: User
    # задачи, созданные/изменённые за этот ход — телеграм-слой покажет по ним карточки с кнопками
    touched: list[Task] = field(default_factory=list)
    # записи памяти, созданные/исправленные за ход (пользователю не показываются — память скрытая, см. /memory)
    remembered: list[Memory] = field(default_factory=list)
    max_memories: int = 150
    # id задач, время которых выбрал бот, а не пользователь — в карточке будет «время выбрал сам»
    auto_time: set[int] = field(default_factory=set)
    # уже просили модель выбрать время за этот ход — второй раз не просим, ставим запасное
    asked_for_time: bool = False
    # от какого момента считать «завтра», «через час»; None = сейчас (для пересланных — дата отправки)
    reference_time: datetime | None = None
    # текст сообщения пользователя и предыдущий ответ бота: по ним проверяем, просил ли он что-то запомнить
    user_text: str = ""
    prev_assistant: str = ""


# Явная просьба запомнить. Посреди разговора факты пишутся в память только по ней (остальное подберёт
# рефлексия после разговора) — модели (GigaChat) это правило из промпта соблюдают ненадёжно, поэтому проверяем в коде. «\bпомни\b», а не «помни», чтобы не ловить «напомни».
EXPLICIT_MEMORY_REQUEST = re.compile(
    r"запомн|запиши себе|сохрани|\bучти|учитывай|имей(те)? в ?виду|\bзнай|\bпомни\b|не забывай",
    re.IGNORECASE,
)
# Бот сам предлагает «Запомнить это?», и короткое согласие на это тоже считается просьбой
MEMORY_OFFER = "Запомнить это?"
AFFIRMATIVE = re.compile(r"^\s*(да|ага|угу|давай|конечно|ок|окей|можно|ну да|yes)\b", re.IGNORECASE)


# Пометка, с которой пересланное сообщение попадает к модели и в историю (её же понимает рефлексия)
FORWARD_MARK = "[Пересланное сообщение"


def memory_requested(ctx: "ToolContext") -> bool:
    if ctx.user_text.startswith(FORWARD_MARK):
        return False  # «запомни» в чужом пересланном сообщении — не просьба пользователя
    if EXPLICIT_MEMORY_REQUEST.search(ctx.user_text):
        return True
    return MEMORY_OFFER.lower() in ctx.prev_assistant.lower() and bool(AFFIRMATIVE.match(ctx.user_text))


class ToolError(Exception):
    """Ошибка, которую стоит показать модели, чтобы она поправилась."""


def task_view(task: Task, tz: str) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "due": format_local(task.due_at, tz) if task.due_at else None,
        "rrule": task.rrule,
        "notes": task.notes,
        "status": task.status,
    }


def _parse_due(ctx: ToolContext, value: str) -> tuple[datetime, bool]:
    """ISO-время от модели -> (UTC, время подставлено кодом)."""
    try:
        local = datetime.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"due_at должен быть ISO 8601, например 2026-10-30T15:00, получено {value!r}") from e
    # Только дата или ровно полночь — модель не выбрала время: ставим как «просто день», а не будим в 00:00
    defaulted = "T" not in value or (local.hour, local.minute, local.second) == (0, 0, 0)
    if defaulted:
        local = local.replace(hour=DEFAULT_TIME.hour, minute=DEFAULT_TIME.minute, second=0)
    return parse_local(local.isoformat(), ctx.user.timezone), defaulted


def _resolve_due(ctx: ToolContext, due_at: str | None, day: str | None, time: str | None,
                 in_minutes: int | None, time_guessed: bool = False) -> tuple[datetime | None, bool]:
    """Время напоминания из того, что дала модель. Даты считает код, а не LLM.

    Возвращает (время, выбрано ли оно без пользователя) — во втором случае бот должен об этом сказать.
    Относительные «завтра», «в пятницу», «через час» считаются от ctx.reference_time — для пересланного
    сообщения это момент его отправки, а не «сейчас».
    """
    now = utcnow()
    base = ctx.reference_time or now
    if in_minutes:
        return base + timedelta(minutes=int(in_minutes)), False
    if due_at:
        due, defaulted = _parse_due(ctx, due_at)
        auto = defaulted or bool(time_guessed)
    elif day or time:
        if day and day not in DAY_KEYWORDS:
            raise ToolError(f"day должен быть одним из {DAY_KEYWORDS}, получено {day!r}")
        try:
            at = parse_hhmm(time) if time else None
        except ValueError as e:
            raise ToolError(f"time нужно в формате HH:MM, получено {time!r}") from e
        due = resolve_day_time(base, ctx.user.timezone, day, at)
        auto = at is None or bool(time_guessed)
    else:
        return None, False
    # Время, выбранное не пользователем, не должно оказаться в прошлом: иначе напоминание придёт сразу
    if auto and due <= now:
        due = nearest_reasonable_time(now, ctx.user.timezone)
    return due, auto


def _due_view(ctx: ToolContext, task: Task, auto: bool) -> dict[str, Any]:
    view = task_view(task, ctx.user.timezone)
    if task.due_at:
        view["in"] = humanize_until(task.due_at, utcnow(), ctx.user.timezone)
    if auto:
        view["time_chosen_by_assistant"] = True
        ctx.auto_time.add(task.id)
    if task.due_at and task.due_at <= utcnow():
        # бывает у пересланных сообщений: срок из них уже прошёл — модель должна предупредить пользователя
        view["already_passed"] = True
    return view


def _check_rrule(value: str | None) -> str | None:
    if not value:
        return None
    value = value.removeprefix("RRULE:")
    try:
        rrulestr(value)
    except (ValueError, TypeError) as e:
        raise ToolError(f"некорректный rrule {value!r}: {e}") from e
    return value


async def _get(ctx: ToolContext, task_id: int) -> Task:
    task = await ctx.repo.get_task(ctx.user.id, int(task_id))
    if not task:
        raise ToolError(f"задача {task_id} не найдена")
    return task


# --- handlers ---


async def create_task(ctx: ToolContext, title: str, due_at: str | None = None, day: str | None = None,
                      time: str | None = None, in_minutes: int | None = None, rrule: str | None = None,
                      notes: str | None = None, time_guessed: bool = False,
                      remind_before_minutes: int | None = None) -> dict:
    rrule = _check_rrule(rrule)
    date_only_due = bool(due_at) and ("T" not in due_at or due_at.split("T", 1)[1].startswith("00:00"))
    if ((day and not time) or date_only_due) and not ctx.asked_for_time:
        # Модели ленятся выбирать время сами — один раз просим подумать, потом сработает запасное время кода
        ctx.asked_for_time = True
        raise ToolError(
            "время не указано. Выбери подходящее сам по смыслу дела и памяти о пользователе (звонки и организации — "
            "рабочее время, покупки и быт — вечер после работы, личное — с учётом распорядка, не ночью) "
            "и вызови create_task ещё раз с time и time_guessed=true"
        )
    due, auto = _resolve_due(ctx, due_at, day, time, in_minutes, time_guessed)
    if rrule and not due:
        raise ToolError("для повторяющейся задачи нужен due_at первого срабатывания")
    event_at = None
    if remind_before_minutes and due:
        # «напомни за день до собрания»: вычитает код, а не модель; когда само событие — в заметке
        event_at = due
        due = event_at - timedelta(minutes=int(remind_before_minutes))
        when = f"Само событие: {format_local(event_at, ctx.user.timezone)}"
        notes = f"{when}. {notes}" if notes else when
    task = await ctx.repo.create_task(ctx.user.id, title=title, due_at=due, rrule=rrule, notes=notes)
    if due and due <= utcnow():
        # срок уже прошёл (обычно из пересланного сообщения): дело видно в сводке как просроченное,
        # но отдельно не звенит — бот и так сказал об этом в ответе
        await ctx.repo.mark_reminded(task.id, utcnow())
    ctx.touched.append(task)
    view = _due_view(ctx, task, auto)
    if event_at:
        # чтобы модель не выдала время напоминания за время события
        view["event_at"] = format_local(event_at, ctx.user.timezone)
        view["remind_at"] = view.pop("due")
    return {"created": view}


async def list_tasks(ctx: ToolContext) -> dict:
    tasks = await ctx.repo.list_open_tasks(ctx.user.id)
    return {"tasks": [task_view(t, ctx.user.timezone) for t in tasks]}


async def update_task(ctx: ToolContext, task_id: int, title: str | None = None, due_at: str | None = None,
                      day: str | None = None, time: str | None = None, in_minutes: int | None = None,
                      rrule: str | None = None, notes: str | None = None, time_guessed: bool = False) -> dict:
    task = await _get(ctx, task_id)
    fields: dict[str, Any] = {}
    auto = False
    if day and not (time or due_at or in_minutes) and task.due_at:
        # «перенеси на пятницу» — другой день, но то же время, что было у дела
        time = to_local(task.due_at, ctx.user.timezone).strftime("%H:%M")
    if title:
        fields["title"] = title
    if time and not (day or due_at or in_minutes) and task.due_at:
        # «перенеси на 12» — тот же день, другое время
        local = to_local(task.due_at, ctx.user.timezone)
        try:
            at = parse_hhmm(time)
        except ValueError as e:
            raise ToolError(f"time нужно в формате HH:MM, получено {time!r}") from e
        fields["due_at"] = local.replace(hour=at.hour, minute=at.minute).astimezone(dt_timezone.utc)
    else:
        due, auto = _resolve_due(ctx, due_at, day, time, in_minutes, time_guessed)
        if due:
            fields["due_at"] = due
    if rrule is not None:
        fields["rrule"] = _check_rrule(rrule)
    if notes is not None:
        fields["notes"] = notes or None
    task = await ctx.repo.update_task(ctx.user.id, int(task_id), **fields)
    ctx.touched.append(task)
    return {"updated": _due_view(ctx, task, auto)}


async def complete_task(ctx: ToolContext, task_id: int) -> dict:
    await _get(ctx, task_id)
    task = await ctx.repo.complete_task(ctx.user.id, int(task_id), ctx.user.timezone)
    if not task:
        raise ToolError(f"задача {task_id} уже закрыта")
    return {"completed": task_view(task, ctx.user.timezone)}


async def cancel_task(ctx: ToolContext, task_id: int) -> dict:
    await _get(ctx, task_id)
    task = await ctx.repo.cancel_task(ctx.user.id, int(task_id))
    return {"cancelled": task_view(task, ctx.user.timezone)}


async def set_timezone(ctx: ToolContext, timezone: str) -> dict:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ToolError(f"неизвестная таймзона {timezone!r}, нужна IANA вида Europe/Moscow") from e
    await ctx.repo.set_timezone(ctx.user.id, timezone)
    ctx.user.timezone = timezone
    return {"timezone": timezone}


def _daily_time(value: str) -> str | None:
    """«08:30» / «8:30» -> "08:30"; «off» -> None (выключить)."""
    value = value.strip().lower()
    if value in ("off", "", "выкл"):
        return None
    try:
        return parse_hhmm(value).strftime("%H:%M")
    except ValueError as e:
        raise ToolError(f"время нужно в формате HH:MM или off, получено {value!r}") from e


async def set_digest_time(ctx: ToolContext, time: str) -> dict:
    normalized = _daily_time(time)
    await ctx.repo.set_digest_time(ctx.user.id, normalized)
    ctx.user.digest_time = normalized
    return {"digest_time": normalized or "off"}


async def set_review_time(ctx: ToolContext, time: str) -> dict:
    normalized = _daily_time(time)
    await ctx.repo.set_review_time(ctx.user.id, normalized)
    ctx.user.review_time = normalized
    return {"review_time": normalized or "off"}


# --- память ---


def memory_view(memory: Memory, tz: str) -> dict[str, Any]:
    view = {"number": memory.number, "kind": memory.kind, "content": memory.content}
    if memory.expires_at:
        view["until"] = format_local(memory.expires_at, tz)
    return view


def _resolve_until(ctx: ToolContext, until_day: str | None, until_date: str | None) -> datetime | None:
    """Конец указанного локального дня: до него временный факт считается актуальным."""
    if until_date:
        try:
            day = date.fromisoformat(until_date)
        except ValueError as e:
            raise ToolError(f"until_date нужен в формате YYYY-MM-DD, получено {until_date!r}") from e
        local = datetime.combine(day, END_OF_DAY, tzinfo=ZoneInfo(ctx.user.timezone))
        return local.astimezone(dt_timezone.utc)
    if until_day:
        if until_day not in DAY_KEYWORDS:
            raise ToolError(f"until_day должен быть одним из {DAY_KEYWORDS}, получено {until_day!r}")
        return resolve_day_time(utcnow(), ctx.user.timezone, until_day, END_OF_DAY)
    return None


def _check_kind(kind: str | None) -> MemoryKind:
    try:
        return MemoryKind(kind or MemoryKind.FACT)
    except ValueError as e:
        raise ToolError(f"kind должен быть одним из {[k.value for k in MemoryKind]}, получено {kind!r}") from e


async def remember(ctx: ToolContext, content: str, kind: str | None = None, until_day: str | None = None,
                   until_date: str | None = None) -> dict:
    content = content.strip()
    if not content:
        raise ToolError("пустой content")
    if not memory_requested(ctx):
        if ctx.user_text.startswith(FORWARD_MARK):
            raise ToolError(
                "это чужое пересланное сообщение — в память пользователя его не сохраняй и НЕ говори, что запомнил. "
                f"Если там полезные данные (телефон, адрес), перескажи их и в конце спроси ровно: «{MEMORY_OFFER}»"
            )
        if ctx.user.auto_memory:
            raise ToolError(
                "пользователь не просил запомнить — посреди разговора ничего не сохраняй и не переспрашивай: "
                "важное из разговора система запомнит сама, когда он закончится"
            )
        raise ToolError(
            "пользователь не просил запомнить — в память пишем только по явной просьбе. Ничего не сохраняй; "
            f"если факт выглядит важным, в конце ответа коротко предложи: «{MEMORY_OFFER}»"
        )
    if len(await ctx.repo.list_memories(ctx.user.id)) >= ctx.max_memories:
        raise ToolError(
            f"память заполнена ({ctx.max_memories} фактов). Скажи пользователю, что сначала нужно удалить "
            "ненужное: список — /memory, удалить — «удали факт N»"
        )
    memory = await ctx.repo.add_memory(
        ctx.user.id, content, kind=_check_kind(kind), source=MemorySource.EXPLICIT,
        expires_at=_resolve_until(ctx, until_day, until_date),
    )
    ctx.remembered.append(memory)
    return {"remembered": memory_view(memory, ctx.user.timezone)}


async def update_memory(ctx: ToolContext, number: int, content: str | None = None, kind: str | None = None,
                        until_day: str | None = None, until_date: str | None = None) -> dict:
    if not await ctx.repo.get_memory(ctx.user.id, int(number)):
        raise ToolError(f"факта №{number} нет в памяти")
    fields: dict[str, Any] = {}
    if content and content.strip():
        fields["content"] = content.strip()
    if kind:
        fields["kind"] = _check_kind(kind)
    if until_day or until_date:
        fields["expires_at"] = _resolve_until(ctx, until_day, until_date)
    memory = await ctx.repo.update_memory(ctx.user.id, int(number), **fields)
    ctx.remembered.append(memory)
    return {"updated": memory_view(memory, ctx.user.timezone)}


async def set_auto_memory(ctx: ToolContext, enabled: bool) -> dict:
    await ctx.repo.set_auto_memory(ctx.user.id, bool(enabled))
    ctx.user.auto_memory = bool(enabled)
    return {"auto_memory": bool(enabled)}


async def forget(ctx: ToolContext, number: int) -> dict:
    memory = await ctx.repo.delete_memory(ctx.user.id, int(number))
    if not memory:
        raise ToolError(f"факта №{number} нет в памяти")
    return {"forgotten": memory.content}


# --- specs ---

_DUE = {
    "type": "string",
    "description": "Только для конкретной календарной даты («15 декабря в 18»): локальное время в ISO 8601 "
                   "без смещения, например 2026-12-15T18:00. Если время не названо — только дата: 2026-12-15. "
                   "Для «завтра», «в пятницу» используй day+time.",
}
_DAY = {
    "type": "string",
    "enum": DAY_KEYWORDS,
    "description": "День напоминания словом: today, tomorrow, day_after_tomorrow или день недели mon..sun "
                   "(ближайший будущий). Дату по дню недели НЕ вычисляй — передай день сюда.",
}
_TIME = {"type": "string", "description": "Время напоминания HH:MM, например 19:00"}
_TIME_GUESSED = {
    "type": "boolean",
    "description": "true, если пользователь НЕ называл время и ты выбрал его сам по смыслу дела",
}
_IN_MINUTES = {"type": "integer", "description": "Для «через N минут/часов»: через сколько минут напомнить"}
_RRULE = {
    "type": "string",
    "description": "Правило повтора RFC 5545 без DTSTART, например FREQ=DAILY или FREQ=WEEKLY;BYDAY=TU,TH. "
                   "Только для повторяющихся дел.",
}
_ID = {"type": "integer", "description": "id задачи"}
_MEMORY_NUMBER = {"type": "integer", "description": "номер факта в памяти (поле number)"}
_KIND = {
    "type": "string",
    "enum": [k.value for k in MemoryKind],
    "description": "person — люди и отношения; preference — предпочтения; routine — распорядок и привычки; "
                   "fact — прочее о пользователе",
}
_UNTIL_DAY = {
    "type": "string",
    "enum": DAY_KEYWORDS,
    "description": "Только для временного факта: до какого дня он актуален (включительно), например «на этой "
                   "неделе в отпуске» → sun. Без этого поля факт бессрочный.",
}
_UNTIL_DATE = {"type": "string", "description": "Как until_day, но конкретной датой YYYY-MM-DD"}

Handler = Callable[..., Awaitable[dict]]

TOOLS: dict[str, tuple[ToolSpec, Handler]] = {
    "create_task": (
        ToolSpec(
            "create_task",
            "Записать новое дело/задачу пользователя, при необходимости с напоминанием и повтором.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Короткая формулировка дела в инфинитиве"},
                    "day": _DAY,
                    "time": _TIME,
                    "time_guessed": _TIME_GUESSED,
                    "in_minutes": _IN_MINUTES,
                    "due_at": _DUE,
                    "rrule": _RRULE,
                    "notes": {"type": "string", "description": "Доп. детали, если есть"},
                    "remind_before_minutes": {
                        "type": "integer",
                        "description": "Если просят напомнить заранее («за день до», «за час»): время СОБЫТИЯ передай "
                                       "в day/time/due_at как есть, а сюда — за сколько минут до него напомнить "
                                       "(за час = 60, за день = 1440). Вычитать сам не нужно.",
                    },
                },
                "required": ["title"],
            },
        ),
        create_task,
    ),
    "list_tasks": (
        ToolSpec("list_tasks", "Получить актуальный список открытых задач пользователя.",
                 {"type": "object", "properties": {}}),
        list_tasks,
    ),
    "update_task": (
        ToolSpec(
            "update_task",
            "Изменить существующую задачу: название, время напоминания, повтор, заметки.",
            {
                "type": "object",
                "properties": {
                    "task_id": _ID,
                    "title": {"type": "string", "description": "Новое название"},
                    "day": _DAY,
                    "time": {**_TIME, "description": "Новое время HH:MM; без day — тот же день, другое время"},
                    "time_guessed": _TIME_GUESSED,
                    "in_minutes": _IN_MINUTES,
                    "due_at": _DUE,
                    "rrule": {**_RRULE, "description": _RRULE["description"] + " Пустая строка снимает повтор."},
                    "notes": {"type": "string", "description": "Новые заметки"},
                },
                "required": ["task_id"],
            },
        ),
        update_task,
    ),
    "complete_task": (
        ToolSpec("complete_task", "Отметить задачу выполненной (повторяющаяся перенесётся на следующий раз).",
                 {"type": "object", "properties": {"task_id": _ID}, "required": ["task_id"]}),
        complete_task,
    ),
    "cancel_task": (
        ToolSpec("cancel_task", "Удалить/отменить задачу, которая больше не нужна.",
                 {"type": "object", "properties": {"task_id": _ID}, "required": ["task_id"]}),
        cancel_task,
    ),
    "set_timezone": (
        ToolSpec("set_timezone", "Сменить часовой пояс пользователя.",
                 {"type": "object",
                  "properties": {"timezone": {"type": "string", "description": "IANA, например Asia/Yekaterinburg"}},
                  "required": ["timezone"]}),
        set_timezone,
    ),
    "set_digest_time": (
        ToolSpec("set_digest_time", "Настроить утреннюю сводку дел: время отправки или выключить.",
                 {"type": "object",
                  "properties": {"time": {"type": "string",
                                          "description": "Локальное время HH:MM, например 08:30, или off чтобы выключить"}},
                  "required": ["time"]}),
        set_digest_time,
    ),
    "set_review_time": (
        ToolSpec("set_review_time",
                 "Настроить вечерний разбор «что переносим?» (несделанное за день с кнопками): время или выключить.",
                 {"type": "object",
                  "properties": {"time": {"type": "string",
                                          "description": "Локальное время HH:MM, например 22:00, или off чтобы выключить"}},
                  "required": ["time"]}),
        set_review_time,
    ),
    "remember": (
        ToolSpec(
            "remember",
            "Сохранить в долговременную память факт о пользователе. Только когда пользователь явно просит "
            "(«запомни», «учти», «имей в виду», «знай, что»). Дела и напоминания сюда не пишутся — для них create_task.",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string",
                                "description": "Факт одной фразой от третьего лица, например «Мама — Ольга, ДР 12 марта»"},
                    "kind": _KIND,
                    "until_day": _UNTIL_DAY,
                    "until_date": _UNTIL_DATE,
                },
                "required": ["content"],
            },
        ),
        remember,
    ),
    "update_memory": (
        ToolSpec(
            "update_memory",
            "Исправить сохранённый факт, если пользователь уточнил или он изменился. Используй вместо remember, "
            "когда о том же уже есть запись в памяти.",
            {
                "type": "object",
                "properties": {
                    "number": _MEMORY_NUMBER,
                    "content": {"type": "string", "description": "Новая формулировка факта"},
                    "kind": _KIND,
                    "until_day": _UNTIL_DAY,
                    "until_date": _UNTIL_DATE,
                },
                "required": ["number"],
            },
        ),
        update_memory,
    ),
    "forget": (
        ToolSpec("forget", "Удалить факт из памяти, если пользователь просит забыть или он больше не верен.",
                 {"type": "object", "properties": {"number": _MEMORY_NUMBER}, "required": ["number"]}),
        forget,
    ),
    "set_auto_memory": (
        ToolSpec(
            "set_auto_memory",
            "Включить или выключить автозапоминание: запоминает ли ассистент сам важное из разговоров "
            "(«не запоминай ничего сам» / «можешь запоминать сам»). Уже сохранённое не трогает.",
            {"type": "object",
             "properties": {"enabled": {"type": "boolean", "description": "true — запоминать сам, false — только по просьбе"}},
             "required": ["enabled"]},
        ),
        set_auto_memory,
    ),
}


async def run_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict:
    entry = TOOLS.get(name)
    if not entry:
        return {"error": f"нет инструмента {name}"}
    _, handler = entry
    try:
        return await handler(ctx, **arguments)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:  # модель передала лишние/не те аргументы
        return {"error": f"неверные аргументы: {e}"}
    except Exception:
        log.exception("tool %s failed", name)
        return {"error": "внутренняя ошибка инструмента"}
