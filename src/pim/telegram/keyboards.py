import html

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pim.core.models import Memory, MemoryKind, Task
from pim.core.timeutils import WEEKDAYS_RU, format_local, to_local
from pim.reminders.digest import Digest

# Лимит Telegram 4096 символов, оставляем запас на разметку
MAX_MESSAGE_CHARS = 3800


class TaskAction(CallbackData, prefix="t"):
    action: str  # done | cancel | snooze
    task_id: int
    minutes: int = 0


class MemoryAction(CallbackData, prefix="m"):
    action: str  # forget — из карточки «Запомнил»; forget_list — из списка /memory
    memory_id: int


MEMORY_SECTIONS = [
    (MemoryKind.PERSON, "👤 Люди"),
    (MemoryKind.ROUTINE, "🔁 Распорядок и привычки"),
    (MemoryKind.PREFERENCE, "⭐ Предпочтения"),
    (MemoryKind.FACT, "📝 Прочее"),
]
# Inline-клавиатура Telegram ограничена 100 кнопками
MAX_MEMORY_BUTTONS = 60


def _memory_line(memory: Memory, tz: str) -> str:
    until = f" <i>(до {format_local(memory.expires_at, tz)})</i>" if memory.expires_at else ""
    return f"{html.escape(memory.content)}{until}"


def memory_card(memory: Memory, tz: str) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(text="Забыть", callback_data=MemoryAction(action="forget", memory_id=memory.id))
    return f"📌 Запомнил: {_memory_line(memory, tz)}", kb.as_markup()


def memory_list(memories: list[Memory], tz: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Всё, что бот знает о пользователе, по разделам; кнопки 🗑 N удаляют запись N."""
    if not memories:
        return ("Я пока ничего о тебе не знаю.\n"
                "Скажи, например: «запомни, что мама — Ольга, день рождения 12 марта».", None)
    parts = ["<b>Что я о тебе знаю</b>"]
    kb = InlineKeyboardBuilder()
    n = 0
    for kind, title in MEMORY_SECTIONS:
        items = [m for m in memories if m.kind == kind]
        if not items:
            continue
        parts.append(f"\n<b>{title}</b>")
        for m in items:
            n += 1
            parts.append(f"{n}. {_memory_line(m, tz)}")
            if n <= MAX_MEMORY_BUTTONS:
                kb.button(text=f"🗑 {n}", callback_data=MemoryAction(action="forget_list", memory_id=m.id))
    parts.append("\nЧтобы удалить запись, нажми 🗑 с её номером или скажи «забудь, что …».")
    kb.adjust(6)
    return _fit(parts), kb.as_markup()


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
