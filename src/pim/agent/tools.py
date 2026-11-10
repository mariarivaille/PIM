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

from pim.core.models import ItemList, Memory, MemoryKind, MemorySource, Task, TaskEventKind, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import (
    DAY_KEYWORDS,
    DEFAULT_TIME,
    format_local,
    humanize_until,
    nearest_reasonable_time,
    parse_local,
    resolve_day_time,
    strip_time_words,
    to_local,
)
from pim.learning.timing import TimeProfile
from pim.lists.matching import display_name, match_items, match_list, normalize
from pim.llm.base import ToolSpec
from pim.sharing.people import find_person
from pim.reminders.daily import parse_hhmm

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
    # уже переспросили модель «чьё это дело», когда в сообщении упомянут близкий
    asked_whose_task: bool = False
    # уже просили переписать поручение от лица исполнителя (без «меня»)
    asked_assign_wording: bool = False
    # поручение, которое пользователь сейчас корректирует (переговоры) — его можно менять, даже если оно не его
    adjust_task: Task | None = None
    # от какого момента считать «завтра», «через час»; None = сейчас (для пересланных — дата отправки)
    reference_time: datetime | None = None
    # день/время в текущем вызове взяты из текста пересланного (а не из подписи пользователя) — считать от reference_time
    date_from_forwarded: bool = True
    # id списков, изменённых или открытых за ход — телеграм-слой покажет их с чекбоксами
    lists_touched: list[int] = field(default_factory=list)
    # привычки по времени (learning/timing.py) — для времени по умолчанию
    profile: TimeProfile | None = None
    # «спланируй мой день»: планировщик и составленный за ход план (телеграм-слой покажет его с кнопками)
    planner: Any = None
    plan: Any = None
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
# Подпись пользователя к пересланному — его собственные слова
COMMENT_MARK = "[Комментарий пользователя к пересланному]"


def memory_requested(ctx: "ToolContext") -> bool:
    text = ctx.user_text
    if text.startswith(FORWARD_MARK):
        # «запомни» в чужом пересланном — не просьба пользователя; в его собственной подписи — просьба
        if COMMENT_MARK not in text:
            return False
        text = text.split(COMMENT_MARK, 1)[1]
    if EXPLICIT_MEMORY_REQUEST.search(text):
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
    # 23:59 — «до конца дня» из дедлайнов: напоминать ночью бессмысленно, это тоже «время не названо»
    defaulted = "T" not in value or (local.hour, local.minute) in ((0, 0), (23, 59))
    if defaulted:
        default = _default_time(ctx, weekend=local.weekday() >= 5)
        local = local.replace(hour=default.hour, minute=default.minute, second=0)
    return parse_local(local.isoformat(), ctx.user.timezone), defaulted


def _default_time(ctx: ToolContext, weekend: bool) -> dt_time:
    """Время, если его не назвали ни пользователь, ни модель: обычный час пользователя, иначе DEFAULT_TIME."""
    hour = ctx.profile.preferred_hour(weekend) if ctx.profile else None
    return dt_time(hour, 0) if hour is not None else DEFAULT_TIME


# Как день из DAY_KEYWORDS звучит в тексте пользователя
_DAY_WORDS = {
    "today": "сегодня", "tomorrow": "завтра", "day_after_tomorrow": "послезавтра", "mon": "понедельник",
    "tue": "вторник", "wed": "сред", "thu": "четверг", "fri": "пятниц", "sat": "суббот", "sun": "воскресен",
}


def _said_in_comment(ctx: ToolContext, day: str | None, in_minutes: int | None) -> bool:
    """День («в пятницу») или «через …» прозвучали в подписи пользователя к пересланному."""
    if COMMENT_MARK not in ctx.user_text:
        return False
    comment = ctx.user_text.split(COMMENT_MARK, 1)[1].casefold()
    if in_minutes:
        return "через" in comment
    word = _DAY_WORDS.get(day or "")
    # «послезавтра» содержит «завтра» — его проверяем отдельно
    if word == "завтра":
        return "завтра" in comment.replace("послезавтра", "")
    return bool(word) and word in comment


def _resolve_due(ctx: ToolContext, due_at: str | None, day: str | None, time: str | None,
                 in_minutes: int | None, time_guessed: bool = False) -> tuple[datetime | None, bool]:
    """Время напоминания из того, что дала модель. Даты считает код, а не LLM.

    Возвращает (время, выбрано ли оно без пользователя) — во втором случае бот должен об этом сказать.
    Относительные «завтра», «в пятницу», «через час» считаются от ctx.reference_time — для пересланного
    сообщения это момент его отправки, а не «сейчас».
    """
    now = utcnow()
    # от даты пересланного — только даты из его текста; слова пользователя в подписи — от «сейчас».
    # Модель флаг date_from_forwarded часто не ставит, поэтому проверяем и сами: есть ли этот день в подписи
    from_comment = not ctx.date_from_forwarded or _said_in_comment(ctx, day, in_minutes)
    base = ctx.reference_time if ctx.reference_time and not from_comment else now
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
        if at is None:
            weekend = to_local(due, ctx.user.timezone).weekday() >= 5
            due = resolve_day_time(base, ctx.user.timezone, day, _default_time(ctx, weekend))
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
    if ctx.adjust_task is not None and int(task_id) == ctx.adjust_task.id:
        # корректировка поручения: поручивший правит дело, которое лежит у исполнителя — только это одно
        return await ctx.repo.get_task(ctx.adjust_task.user_id, int(task_id))
    task = await ctx.repo.get_task(ctx.user.id, int(task_id))
    if not task:
        raise ToolError(f"задача {task_id} не найдена")
    return task


# --- handlers ---


async def create_task(ctx: ToolContext, title: str, due_at: str | None = None, day: str | None = None,
                      time: str | None = None, in_minutes: int | None = None, rrule: str | None = None,
                      notes: str | None = None, time_guessed: bool = False,
                      remind_before_minutes: int | None = None, date_from_forwarded: bool = True) -> dict:
    ctx.date_from_forwarded = date_from_forwarded
    rrule = _check_rrule(rrule)
    if not ctx.asked_whose_task and (person := await _mentioned_person(ctx)):
        # «Андрей должен позвать меня на кофе» модель записывала как дело пользователя. Запретить create_task
        # при упоминании нельзя («позвонить Андрею» — дело самого пользователя), поэтому один раз переспрашиваем.
        ctx.asked_whose_task = True
        raise ToolError(
            f"в сообщении упомянут близкий ({person}). Если это дело должен сделать {person} — вызови assign_task "
            f"(title от его лица). Если дело самого пользователя (например, «позвонить {person}») — повтори create_task"
        )
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
    if due:
        title = strip_time_words(title)  # время — в сроке, в названии оно устареет при первом переносе
    task = await ctx.repo.create_task(ctx.user.id, title=title, due_at=due, rrule=rrule, notes=notes)
    if due and due <= utcnow():
        # срок уже прошёл (обычно из пересланного сообщения): дело видно в сводке как просроченное,
        # но отдельно не звенит — бот и так сказал об этом в ответе
        await ctx.repo.mark_reminded(task.id, utcnow(), sent=False)
    ctx.touched.append(task)
    if due and not auto and not in_minutes and due > utcnow():
        # пользователь сам назвал время — сигнал, в какие часы ему удобно (см. learning/timing.py)
        await ctx.repo.log_event(ctx.user.id, TaskEventKind.PLANNED, event_at or due, task_id=task.id)
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
    if fields.get("title") or "due_at" in fields:
        # «обнять в обед», перенесённое на 20:00, — противоречие: время в названии убираем
        cleaned = strip_time_words(fields.get("title") or task.title)
        if cleaned != task.title:
            fields["title"] = cleaned
    task = await ctx.repo.update_task(task.user_id, int(task_id), **fields)
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


async def plan_day(ctx: ToolContext, day: str = "today") -> dict:
    if ctx.planner is None:
        raise ToolError("планирование недоступно")
    if day not in DAY_KEYWORDS:
        raise ToolError(f"day должен быть одним из {DAY_KEYWORDS}, получено {day!r}")
    target = to_local(resolve_day_time(utcnow(), ctx.user.timezone, day, dt_time(12, 0)), ctx.user.timezone).date()
    plan = await ctx.planner.plan(ctx.user, target)
    ctx.plan = plan
    tz = ctx.user.timezone
    return {
        "day": f"{target:%d.%m}",
        "planned": [{"time": f"{to_local(i.at, tz):%H:%M}", "title": i.task.title} for i in plan.items],
        "not_fitted": [t.title for t in plan.skipped],
        "note": "План с кнопкой «Поставить напоминания» пользователь увидит отдельным сообщением. "
                "Ответь одной фразой: план ниже, подходит ли. Сам ничего не переставляй."
                if plan.items else "Планировать нечего: дел без времени и просроченных нет — так и скажи.",
    }


# --- близкие ---


async def _find_person(ctx: ToolContext, person: str):
    people = await ctx.repo.connections(ctx.user.id)
    if not people:
        raise ToolError("у пользователя пока нет близких в Pim — скажи, что пригласить можно командой /invite")
    found = find_person(people, person)
    if found is None:
        names = ", ".join(c.alias for c, _ in people)
        raise ToolError(f"«{person}» нет среди близких в Pim (есть: {names}). Pim умеет передавать только тем, "
                        "кто в нём есть, — не предлагай телефон, почту и т.п.; скажи, что пригласить можно командой /invite")
    return found


FIRST_PERSON = re.compile(r"(?<!\w)(меня|мне|мной|мною)(?!\w)", re.IGNORECASE)


async def _mentioned_person(ctx: ToolContext) -> str | None:
    """Имя близкого, упомянутого в сообщении пользователя (в любом падеже), или None."""
    if ctx.user_text.startswith(FORWARD_MARK):
        return None  # в пересланном чужом тексте имена — не повод переспрашивать
    people = await ctx.repo.connections(ctx.user.id)
    if not people:
        return None
    for word in re.findall(r"[^\W\d_]{3,}", ctx.user_text):
        if found := find_person(people, word):
            return found[0].alias
    return None


# Два отдельных инструмента, а не один с параметром «кого»: с ним модель путала направление
# («милый (я так зову Андрея)» превращалось в «Андрей видит меня как милый»).

def _clean_name(name: str) -> str:
    name = name.strip()[:100]
    if not name:
        raise ToolError("пустое имя")
    return name


async def add_person_name(ctx: ToolContext, person: str, name: str) -> dict:
    conn, peer = await _find_person(ctx, person)
    name = _clean_name(name)
    added = await ctx.repo.add_nickname(ctx.user.id, peer.id, name)
    return {"person": conn.alias, "added_name": name, "already_known": not added}


async def remove_person_name(ctx: ToolContext, person: str, name: str) -> dict:
    conn, peer = await _find_person(ctx, person)
    if normalize(name) == normalize(conn.alias):
        raise ToolError(f"«{name}» — основное имя; чтобы сменить его, используй call_person")
    if not await ctx.repo.remove_nickname(ctx.user.id, peer.id, name):
        raise ToolError(f"у {conn.alias} нет имени «{name}»")
    return {"person": conn.alias, "removed_name": name}


async def call_person(ctx: ToolContext, person: str, new_name: str) -> dict:
    conn, peer = await _find_person(ctx, person)
    name = _clean_name(new_name)
    await ctx.repo.set_alias(ctx.user.id, peer.id, name)
    return {"now": f"основное имя теперь «{name}», прежнее «{conn.alias}» тоже остаётся"}


async def show_me_as(ctx: ToolContext, person: str, my_name: str) -> dict:
    conn, peer = await _find_person(ctx, person)
    name = _clean_name(my_name)
    await ctx.repo.set_alias(peer.id, ctx.user.id, name)
    return {"now": f"{conn.alias} теперь видит пользователя как «{name}»"}


async def share_list(ctx: ToolContext, list: str, person: str) -> dict:
    lst = await _find_list(ctx, list)
    if lst.user_id != ctx.user.id:
        raise ToolError("это чужой общий список — поделиться им может только владелец")
    conn, peer = await _find_person(ctx, person)
    added = await ctx.repo.add_list_member(lst.id, peer.id)
    return {"shared_list": lst.name, "with": conn.alias, "already_shared": not added}


async def assign_task(ctx: ToolContext, person: str, title: str, due_at: str | None = None, day: str | None = None,
                      time: str | None = None, in_minutes: int | None = None, notes: str | None = None,
                      date_from_forwarded: bool = True) -> dict:
    ctx.date_from_forwarded = date_from_forwarded
    conn, peer = await _find_person(ctx, person)
    if not ctx.asked_assign_wording and FIRST_PERSON.search(title):
        # «позвать меня на кофе» у исполнителя читается так, будто звать надо его самого
        ctx.asked_assign_wording = True
        me = await ctx.repo.display_name(peer.id, ctx.user.id)
        raise ToolError(f"title увидит {conn.alias}, поэтому «меня/мне/мной» замени на имя пользователя ({me}) "
                        f"в нужном падеже и повтори assign_task")
    due, _ = _resolve_due(ctx, due_at, day, time, in_minutes)
    if due:
        title = strip_time_words(title)
    # от кого — в заметке (видно и в карточке, и в напоминании, и в /tasks); имя — как исполнитель зовёт пользователя
    who = f"Просьба от {await ctx.repo.display_name(peer.id, ctx.user.id)}"
    task = await ctx.repo.create_assigned_task(peer.id, ctx.user.id, title=title, due_at=due,
                                               notes=f"{who}. {notes}" if notes else who)
    return {"assigned_to": conn.alias, "title": task.title,
            "due": format_local(due, ctx.user.timezone) if due else None,
            "note": f"{conn.alias} получит сообщение сейчас" + (" и напоминание в срок" if due else "")
                    + "; когда сделает — пользователю придёт уведомление"}


async def set_auto_memory(ctx: ToolContext, enabled: bool) -> dict:
    await ctx.repo.set_auto_memory(ctx.user.id, bool(enabled))
    ctx.user.auto_memory = bool(enabled)
    return {"auto_memory": bool(enabled)}


async def forget(ctx: ToolContext, number: int) -> dict:
    memory = await ctx.repo.delete_memory(ctx.user.id, int(number))
    if not memory:
        raise ToolError(f"факта №{number} нет в памяти")
    return {"forgotten": memory.content}


# --- списки ---

MAX_ITEM_CHARS = 200


def _items_arg(items: Any) -> list[str]:
    """Модель обычно шлёт массив, но бывает и строка «молоко, хлеб и яйца»."""
    if isinstance(items, str):
        items = re.split(r",(?!\d)|[;\n]|\s+и\s+", items)  # «молоко 2,5%» не режем
    return [str(i).strip()[:MAX_ITEM_CHARS] for i in items or [] if str(i).strip()]


async def _find_list(ctx: ToolContext, name: str) -> ItemList:
    lists = [lst for lst, _, _ in await ctx.repo.user_lists(ctx.user.id)]
    lst = match_list(lists, name)
    if lst is None:
        known = ", ".join(f"«{l.name}»" for l in lists) or "списков пока нет"
        raise ToolError(f"списка «{name}» нет. Есть: {known}")
    return lst


async def _list_view(ctx: ToolContext, lst: ItemList) -> dict[str, Any]:
    items = await ctx.repo.list_items(lst.id)
    if lst.id not in ctx.lists_touched:
        ctx.lists_touched.append(lst.id)
    return {"list": lst.name, "items": [{"text": i.text, "done": i.done} for i in items]}


async def add_to_list(ctx: ToolContext, list: str, items: Any) -> dict:
    texts = _items_arg(items)
    if not texts:
        raise ToolError("нечего добавлять: items пустой")
    lists = [lst for lst, _, _ in await ctx.repo.user_lists(ctx.user.id)]
    lst = match_list(lists, list)
    created = lst is None
    if created:
        lst = await ctx.repo.create_list(ctx.user.id, display_name(list))
    open_items = [i for i in await ctx.repo.list_items(lst.id) if not i.done]
    already = {normalize(i.text) for i in open_items}
    new, duplicates = [], []
    for text in texts:
        (duplicates if normalize(text) in already else new).append(text)
        already.add(normalize(text))
    await ctx.repo.add_items(lst.id, new, added_by=ctx.user.id)
    view = await _list_view(ctx, lst)
    return {**view, "created_list": created, "added": new, "already_there": duplicates}


async def check_off(ctx: ToolContext, list: str, items: Any) -> dict:
    lst = await _find_list(ctx, list)
    open_items = [i for i in await ctx.repo.list_items(lst.id) if not i.done]
    found, missing = match_items(open_items, _items_arg(items))
    await ctx.repo.set_items_done(lst.id, [i.id for i in found], True)
    return {**await _list_view(ctx, lst), "checked": [i.text for i in found], "not_found": missing}


async def remove_from_list(ctx: ToolContext, list: str, items: Any) -> dict:
    lst = await _find_list(ctx, list)
    found, missing = match_items(await ctx.repo.list_items(lst.id), _items_arg(items))
    await ctx.repo.remove_items(lst.id, [i.id for i in found])
    return {**await _list_view(ctx, lst), "removed": [i.text for i in found], "not_found": missing}


async def show_list(ctx: ToolContext, list: str) -> dict:
    return await _list_view(ctx, await _find_list(ctx, list))


async def delete_list(ctx: ToolContext, list: str) -> dict:
    lst = await _find_list(ctx, list)
    if await ctx.repo.delete_list(ctx.user.id, lst.id) is None:
        raise ToolError("это общий список другого человека — удалить его может только владелец")
    if lst.id in ctx.lists_touched:
        ctx.lists_touched.remove(lst.id)
    return {"deleted_list": lst.name}


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
_FROM_FORWARDED = {
    "type": "boolean",
    "description": "Только для пересланных сообщений: true — день/время взяты из текста пересланного («завтра» в нём "
                   "считается от даты отправки); false — из подписи самого пользователя (считается от «сейчас»)",
}
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
_LIST = {"type": "string",
         "description": "Короткое название списка в именительном падеже, без слова «список»: «покупки» "
                        "(даже если сказали «в список покупок»), «в поездку», «фильмы», «подарки»"}
# Строка, а не массив: с массивом GigaChat добавляет по одному пункту за вызов (N запросов к модели вместо одного)
_ITEMS = {"type": "string",
          "description": "ВСЕ пункты из фразы одной строкой через запятую: «молоко, хлеб, яйца»"}
_PERSON = {"type": "string", "description": "Имя близкого, как его назвал пользователь: «Андрей», «мама»"}
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
                    "date_from_forwarded": _FROM_FORWARDED,
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
    "add_to_list": (
        ToolSpec("add_to_list",
                 "Добавить пункты в список (покупки, вещи в поездку, фильмы…). Списка нет — он создастся.",
                 {"type": "object", "properties": {"list": _LIST, "items": _ITEMS}, "required": ["list", "items"]}),
        add_to_list,
    ),
    "check_off": (
        ToolSpec("check_off", "Отметить пункты списка сделанными/купленными («купила молоко»).",
                 {"type": "object", "properties": {"list": _LIST, "items": _ITEMS}, "required": ["list", "items"]}),
        check_off,
    ),
    "remove_from_list": (
        ToolSpec("remove_from_list", "Удалить пункты из списка (не отметить, а убрать совсем).",
                 {"type": "object", "properties": {"list": _LIST, "items": _ITEMS}, "required": ["list", "items"]}),
        remove_from_list,
    ),
    "show_list": (
        ToolSpec("show_list", "Показать список целиком («что в покупках?»).",
                 {"type": "object", "properties": {"list": _LIST}, "required": ["list"]}),
        show_list,
    ),
    "delete_list": (
        ToolSpec("delete_list", "Удалить список целиком — только если пользователь прямо просит.",
                 {"type": "object", "properties": {"list": _LIST}, "required": ["list"]}),
        delete_list,
    ),
    "plan_day": (
        ToolSpec("plan_day",
                 "«Спланируй мой день / распланируй завтра»: разложить дела без времени и просроченные по свободным "
                 "окнам дня с учётом распорядка и привычек. Пользователь подтвердит план кнопкой.",
                 {"type": "object",
                  "properties": {"day": {**_DAY, "description": "Какой день планировать: today (по умолчанию), "
                                                                "tomorrow или день недели"}}}),
        plan_day,
    ),
    "share_list": (
        ToolSpec("share_list", "Сделать список общим с близким человеком: он увидит его и сможет отмечать и добавлять.",
                 {"type": "object",
                  "properties": {"list": _LIST, "person": _PERSON},
                  "required": ["list", "person"]}),
        share_list,
    ),
    "add_person_name": (
        ToolSpec(
            "add_person_name",
            "Добавить близкому ещё одно имя, как его зовёт САМ ПОЛЬЗОВАТЕЛЬ: «я зову Андрея милый», "
            "«милый — это Андрей», «Андрей — это ещё и Андрюша». Остальные имена сохраняются.",
            {"type": "object",
             "properties": {"person": _PERSON,
                            "name": {"type": "string", "description": "Ещё одно имя близкого"}},
             "required": ["person", "name"]},
        ),
        add_person_name,
    ),
    "remove_person_name": (
        ToolSpec("remove_person_name", "Убрать одно из имён близкого: «больше не зови Андрея зай».",
                 {"type": "object",
                  "properties": {"person": _PERSON, "name": {"type": "string", "description": "Какое имя убрать"}},
                  "required": ["person", "name"]}),
        remove_person_name,
    ),
    "call_person": (
        ToolSpec(
            "call_person",
            "Сменить ОСНОВНОЕ имя близкого — то, которым он подписан в сообщениях пользователю. Только по явной "
            "просьбе: «переименуй Андрея в Андрюшу», «пусть Андрей везде будет Андрюша». Прежнее имя сохранится.",
            {"type": "object",
             "properties": {"person": _PERSON,
                            "new_name": {"type": "string", "description": "Новое основное имя"}},
             "required": ["person", "new_name"]},
        ),
        call_person,
    ),
    "show_me_as": (
        ToolSpec(
            "show_me_as",
            "Как БЛИЗКИЙ будет видеть ПОЛЬЗОВАТЕЛЯ в своих сообщениях: «пусть Андрей видит меня как Маша», "
            "«для Андрея я Маша». Только если пользователь говорит о своём имени.",
            {"type": "object",
             "properties": {"person": _PERSON,
                            "my_name": {"type": "string", "description": "Имя пользователя, которое увидит близкий"}},
             "required": ["person", "my_name"]},
        ),
        show_me_as,
    ),
    "assign_task": (
        ToolSpec(
            "assign_task",
            "Поручить дело близкому («напомни Андрею купить хлеб вечером»): дело появится у него, он получит "
            "сообщение и напоминание, а пользователю придёт уведомление, когда тот сделает.",
            {
                "type": "object",
                "properties": {
                    "person": _PERSON,
                    "title": {"type": "string", "description": "Что сделать, в инфинитиве: «купить хлеб»"},
                    "day": _DAY,
                    "time": _TIME,
                    "in_minutes": _IN_MINUTES,
                    "due_at": _DUE,
                    "notes": {"type": "string", "description": "Детали, если есть"},
                    "date_from_forwarded": _FROM_FORWARDED,
                },
                "required": ["person", "title"],
            },
        ),
        assign_task,
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
