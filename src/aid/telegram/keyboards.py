import html

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aid.core.models import Task
from aid.core.timeutils import WEEKDAYS_RU, format_local, to_local
from aid.reminders.digest import Digest

# Лимит Telegram 4096 символов, оставляем запас на разметку
MAX_MESSAGE_CHARS = 3800


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


def _digest_line(task: Task, tz: str, with_date: bool) -> str:
    when = ""
    if task.due_at:
        local = to_local(task.due_at, tz)
        when = format_local(task.due_at, tz) if with_date else f"{local:%H:%M}"
        when = f"{when} — "
    rep = " 🔁" if task.rrule else ""
    return f"• {when}{html.escape(task.title)}{rep}"


def format_digest(digest: Digest, tz: str) -> str:
    day = digest.day
    parts = [f"☀️ <b>Доброе утро! План на {WEEKDAYS_RU[day.weekday()]} {day:%d.%m}</b>"]
    if digest.is_empty:
        parts.append("Дел нет — свободный день 🎉")
        return "\n".join(parts)
    if digest.overdue:
        parts.append("\n<b>Просрочено:</b>")
        parts += [_digest_line(t, tz, with_date=True) for t in digest.overdue]
    if digest.today:
        parts.append("\n<b>Сегодня:</b>")
        parts += [_digest_line(t, tz, with_date=False) for t in digest.today]
    else:
        parts.append("\nНа сегодня ничего не запланировано.")
    if digest.upcoming:
        parts.append("\n<b>Дальше:</b>")
        parts += [_digest_line(t, tz, with_date=True) for t in digest.upcoming]
    if digest.undated:
        parts.append("\n<b>Без срока:</b>")
        parts += [_digest_line(t, tz, with_date=False) for t in digest.undated]
    return _fit(parts)


def _fit(lines: list[str]) -> str:
    """Обрезает сводку по целым строкам, чтобы влезть в лимит сообщения Telegram."""
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > MAX_MESSAGE_CHARS:
            out.append(f"…и ещё {sum(1 for l in lines[i:] if l.startswith('•'))} дел, не влезло в сообщение")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


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
