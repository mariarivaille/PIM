import html

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pim.core.models import Memory, MemoryKind, MemorySource, Task, TaskStatus, utcnow
from pim.core.timeutils import WEEKDAYS_RU, format_local, humanize_until, to_local
from pim.memory.reflection import ReflectionResult
from pim.reminders.digest import Digest

# Лимит Telegram 4096 символов, оставляем запас на разметку
MAX_MESSAGE_CHARS = 3800


class TaskAction(CallbackData, prefix="t"):
    action: str  # done | cancel | snooze
    task_id: int
    minutes: int = 0


MEMORY_SECTIONS = [
    (MemoryKind.PERSON, "👤 Люди"),
    (MemoryKind.ROUTINE, "🔁 Распорядок и привычки"),
    (MemoryKind.PREFERENCE, "⭐ Предпочтения"),
    (MemoryKind.FACT, "📝 Прочее"),
]


def _memory_line(memory: Memory, tz: str) -> str:
    until = f" <i>(до {format_local(memory.expires_at, tz)})</i>" if memory.expires_at else ""
    auto = " <i>(сам)</i>" if memory.source == MemorySource.INFERRED else ""
    return f"{memory.number}. {html.escape(memory.content)}{until}{auto}"


def memory_card(memory: Memory, tz: str) -> str:
    return f"📌 Запомнил — {_memory_line(memory, tz)}"


AUTO_MEMORY_HINT = {
    True: "Важное из разговоров запоминаю сам (помечено «сам»). Выключить: «не запоминай ничего сам».",
    False: "Сам ничего не запоминаю, только по просьбе. Включить: «запоминай сам».",
}


def memory_list(memories: list[Memory], tz: str, auto_memory: bool = True) -> str:
    """Всё, что бот знает о пользователе, по разделам, с номерами для «удали факт N»."""
    if not memories:
        return ("Я пока ничего о тебе не знаю.\n"
                "Скажи, например: «запомни, что мама — Ольга, день рождения 12 марта».\n\n"
                + AUTO_MEMORY_HINT[auto_memory])
    parts = ["<b>Что я о тебе знаю</b>"]
    for kind, title in MEMORY_SECTIONS:
        items = [m for m in memories if m.kind == kind]
        if items:
            parts.append(f"\n<b>{title}</b>")
            parts += [_memory_line(m, tz) for m in items]
    parts.append("\nЧтобы удалить, напиши «удали факт 3» (можно несколько: «удали факт 3, 5»).")
    parts.append(AUTO_MEMORY_HINT[auto_memory])
    return _fit(parts)


def memory_update_text(result: ReflectionResult, tz: str) -> str:
    """Сообщение после рефлексии: что бот запомнил сам и что исправил."""
    parts = []
    if result.added:
        parts.append("🧠 <b>Запомнил из разговора</b>")
        parts += [_memory_line(m, tz) for m in result.added]
    if result.updated:
        parts.append(("\n" if parts else "") + "✏️ <b>Уточнил</b>")
        parts += [_memory_line(m, tz) for m in result.updated]
    parts.append("\nЕсли что-то лишнее — «удали факт N». Не запоминать самому — «не запоминай ничего сам».")
    return "\n".join(parts)


SNOOZE_OPTIONS = [(15, "+15 мин"), (60, "+1 ч"), (24 * 60, "Завтра")]


def task_card(task: Task, tz: str, auto_time: bool = False) -> str:
    parts = [f"<b>{html.escape(task.title)}</b>"]
    if task.due_at:
        until = humanize_until(task.due_at, utcnow(), tz) if task.status == TaskStatus.OPEN else ""
        parts.append(f"⏰ {format_local(task.due_at, tz)}" + (f" · {until}" if until else ""))
    if task.rrule:
        parts.append(f"🔁 {html.escape(task.rrule)}")
    if task.notes:
        parts.append(html.escape(task.notes))
    if auto_time and task.due_at:
        parts.append("<i>Время выбрал сам — если нужно другое, скажи, например «перенеси на 15:00».</i>")
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
