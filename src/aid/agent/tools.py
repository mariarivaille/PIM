"""Инструменты агента. Каждый инструмент = ToolSpec для LLM + async-обработчик.

Чтобы добавить новое умение (календарь, почта, ...) — допиши спеку и обработчик в TOOLS.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from aid.core.models import Task, User
from aid.core.repository import Repository
from aid.core.timeutils import format_local, parse_local
from aid.llm.base import ToolSpec
from aid.reminders.digest import parse_hhmm

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    repo: Repository
    user: User
    # задачи, созданные/изменённые за этот ход — телеграм-слой покажет по ним карточки с кнопками
    touched: list[Task] = field(default_factory=list)


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


def _parse_due(ctx: ToolContext, value: str | None):
    if not value:
        return None
    try:
        return parse_local(value, ctx.user.timezone)
    except ValueError as e:
        raise ToolError(f"due_at должен быть ISO 8601, например 2026-10-30T15:00, получено {value!r}") from e


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


async def create_task(ctx: ToolContext, title: str, due_at: str | None = None, rrule: str | None = None,
                      notes: str | None = None) -> dict:
    rrule = _check_rrule(rrule)
    due = _parse_due(ctx, due_at)
    if rrule and not due:
        raise ToolError("для повторяющейся задачи нужен due_at первого срабатывания")
    task = await ctx.repo.create_task(ctx.user.id, title=title, due_at=due, rrule=rrule, notes=notes)
    ctx.touched.append(task)
    return {"created": task_view(task, ctx.user.timezone)}


async def list_tasks(ctx: ToolContext) -> dict:
    tasks = await ctx.repo.list_open_tasks(ctx.user.id)
    return {"tasks": [task_view(t, ctx.user.timezone) for t in tasks]}


async def update_task(ctx: ToolContext, task_id: int, title: str | None = None, due_at: str | None = None,
                      rrule: str | None = None, notes: str | None = None) -> dict:
    await _get(ctx, task_id)
    fields: dict[str, Any] = {}
    if title:
        fields["title"] = title
    if due_at:
        fields["due_at"] = _parse_due(ctx, due_at)
    if rrule is not None:
        fields["rrule"] = _check_rrule(rrule)
    if notes is not None:
        fields["notes"] = notes or None
    task = await ctx.repo.update_task(ctx.user.id, int(task_id), **fields)
    ctx.touched.append(task)
    return {"updated": task_view(task, ctx.user.timezone)}


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


async def set_digest_time(ctx: ToolContext, time: str) -> dict:
    value = time.strip().lower()
    if value in ("off", "", "выкл"):
        await ctx.repo.set_digest_time(ctx.user.id, None)
        ctx.user.digest_time = None
        return {"digest": "off"}
    try:
        parsed = parse_hhmm(value)
    except ValueError as e:
        raise ToolError(f"время сводки нужно в формате HH:MM, получено {time!r}") from e
    normalized = parsed.strftime("%H:%M")
    await ctx.repo.set_digest_time(ctx.user.id, normalized)
    ctx.user.digest_time = normalized
    return {"digest_time": normalized}


# --- specs ---

_DUE = {
    "type": "string",
    "description": "Когда напомнить, локальное время пользователя в ISO 8601 без смещения, например 2026-10-30T15:00",
}
_RRULE = {
    "type": "string",
    "description": "Правило повтора RFC 5545 без DTSTART, например FREQ=DAILY или FREQ=WEEKLY;BYDAY=TU,TH. "
                   "Только для повторяющихся дел.",
}
_ID = {"type": "integer", "description": "id задачи"}

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
                    "due_at": _DUE,
                    "rrule": _RRULE,
                    "notes": {"type": "string", "description": "Доп. детали, если есть"},
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
