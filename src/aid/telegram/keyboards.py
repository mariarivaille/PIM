import html

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aid.core.models import Task
from aid.core.timeutils import format_local


class TaskAction(CallbackData, prefix="t"):
    action: str  # done | cancel | snooze
    task_id: int
    minutes: int = 0


SNOOZE_OPTIONS = [(15, "+15 мин"), (60, "+1 ч"), (24 * 60, "Завтра")]


def task_card(task: Task, tz: str) -> str:
    parts = [f"<b>{html.escape(task.title)}</b>"]
    if task.due_at:
        parts.append(f"⏰ {format_local(task.due_at, tz)}")
    if task.rrule:
        parts.append(f"🔁 {html.escape(task.rrule)}")
    if task.notes:
        parts.append(html.escape(task.notes))
    return "\n".join(parts)


def created_kb(task: Task) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано", callback_data=TaskAction(action="done", task_id=task.id))
    kb.button(text="🗑 Удалить", callback_data=TaskAction(action="cancel", task_id=task.id))
    return kb.as_markup()


def reminder_kb(task: Task) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано", callback_data=TaskAction(action="done", task_id=task.id))
    for minutes, label in SNOOZE_OPTIONS:
        kb.button(text=label, callback_data=TaskAction(action="snooze", task_id=task.id, minutes=minutes))
    kb.adjust(1, 3)
    return kb.as_markup()
