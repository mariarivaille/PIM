import html

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pim.core.models import ItemList, ListItem, Memory, MemoryKind, MemorySource, Task, TaskStatus, utcnow
from pim.core.timeutils import WEEKDAYS_RU, format_local, humanize_until, plural_ru, to_local
from pim.learning.timing import MIN_HOUR_SAMPLES, MIN_SNOOZE_SAMPLES, WINDOW_DAYS, TimeProfile
from pim.lists.matching import normalize
from pim.reminders.dates import UpcomingDate, days_until_text
from pim.reminders.digest import Digest
from pim.reminders.review import Review

# Лимит Telegram 4096 символов, оставляем запас на разметку
MAX_MESSAGE_CHARS = 3800


class TaskAction(CallbackData, prefix="t"):
    action: str  # done | cancel | snooze | tomorrow
    task_id: int
    minutes: int = 0


class ReviewAction(CallbackData, prefix="r"):
    action: str  # all_tomorrow


class DateAction(CallbackData, prefix="d"):
    action: str  # gift | congrats | skip
    memory_id: int
    occurs: int  # YYYYMMDD наступления даты
    done: int = 0  # битовая маска уже нажатых кнопок: так клавиатура без состояния в БД


DATE_GIFT, DATE_CONGRATS = 1, 2
_BIRTHDAY = ("др", "д.р", "день рожд", "дня рожд", "днюх", "родил")


def _date_icon(content: str) -> str:
    text = content.casefold()
    if any(k in text for k in ("свадьб", "годовщин")):
        return "💍"
    return "🎂" if any(k in text for k in _BIRTHDAY) else "🎉"


def _age_text(up: UpcomingDate) -> str:
    if up.age is None:
        return ""
    if any(k in up.memory.content.casefold() for k in _BIRTHDAY):
        return f" — исполнится {up.age}"
    return f" — {up.age} {plural_ru(up.age, 'год', 'года', 'лет')}"


def date_alert_kb(up: UpcomingDate, done: int = 0) -> InlineKeyboardMarkup | None:
    if up.days == 0:
        return None
    occurs = int(up.occurs.strftime("%Y%m%d"))
    kb = InlineKeyboardBuilder()
    if not done & DATE_GIFT:
        kb.button(text="🎁 Напомнить купить подарок",
                  callback_data=DateAction(action="gift", memory_id=up.memory.id, occurs=occurs, done=done))
    if not done & DATE_CONGRATS:
        kb.button(text="📞 Напомнить поздравить",
                  callback_data=DateAction(action="congrats", memory_id=up.memory.id, occurs=occurs, done=done))
    if done == DATE_GIFT | DATE_CONGRATS:
        return None
    kb.button(text="👌 Не нужно", callback_data=DateAction(action="skip", memory_id=up.memory.id, occurs=occurs))
    kb.adjust(1)
    return kb.as_markup()


def date_alert_text(up: UpcomingDate) -> str:
    when = f"{WEEKDAYS_RU[up.occurs.weekday()]} {up.occurs:%d.%m}"
    what = f"{html.escape(up.memory.content)}{_age_text(up)}"
    icon = _date_icon(up.memory.content)
    if up.days == 0:
        return f"{icon} <b>Сегодня</b> — {what}\nНе забудь поздравить!"
    if up.days == 1:
        return f"{icon} <b>Завтра, {when}</b> — {what}"
    return f"{icon} <b>{days_until_text(up.days).capitalize()}, {when}</b> — {what}\nПодготовиться заранее?"


class ListAction(CallbackData, prefix="l"):
    action: str  # toggle | clear | open
    list_id: int
    item_id: int = 0


# Inline-клавиатура Telegram ограничена 100 кнопками
MAX_LIST_BUTTONS = 90
LIST_ICONS = [(("покуп", "продукт", "магаз"), "🛒"), (("поезд", "отпуск", "путеш", "дорог"), "🧳"),
              (("фильм", "кино", "сериал"), "🎬"), (("подар",), "🎁"), (("книг", "почитат"), "📚")]


def _list_icon(name: str) -> str:
    n = normalize(name)
    return next((icon for keys, icon in LIST_ICONS if any(n.startswith(k) or f" {k}" in f" {n}" for k in keys)), "📋")


def list_card(lst: ItemList, items: list[ListItem]) -> tuple[str, InlineKeyboardMarkup]:
    """Список как сообщение с чекбоксами: нажатие на пункт отмечает его, «🧹» убирает отмеченные."""
    done = sum(i.done for i in items)
    title = f"{_list_icon(lst.name)} <b>{html.escape(lst.name)}</b>"
    if not items:
        text = f"{title}\nПусто — скажи, например: «добавь в {html.escape(lst.name.lower())} …»"
    else:
        text = f"{title} · отмечено {done} из {len(items)}"
    kb = InlineKeyboardBuilder()
    for item in items[:MAX_LIST_BUTTONS]:
        mark = "✅" if item.done else "☐"
        label = item.text if len(item.text) <= 60 else item.text[:57] + "…"
        kb.button(text=f"{mark} {label}", callback_data=ListAction(action="toggle", list_id=lst.id, item_id=item.id))
    if done:
        kb.button(text="🧹 Убрать отмеченные", callback_data=ListAction(action="clear", list_id=lst.id))
    kb.adjust(1)
    if len(items) > MAX_LIST_BUTTONS:
        text += f"\nПоказаны первые {MAX_LIST_BUTTONS} пунктов."
    return text, kb.as_markup()


def lists_menu(lists: list[tuple[ItemList, int, int]]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not lists:
        return ("Списков пока нет. Скажи, например: «добавь в покупки молоко и хлеб» или "
                "«в поездку взять зарядку и паспорт» — список создастся сам.", None)
    kb = InlineKeyboardBuilder()
    for lst, open_, _ in lists:
        kb.button(text=f"{_list_icon(lst.name)} {lst.name} ({open_})",
                  callback_data=ListAction(action="open", list_id=lst.id))
    kb.adjust(1)
    return "Твои списки (в скобках — сколько не отмечено):", kb.as_markup()


# Сколько дел показывать карточками в вечернем разборе; остальное — в /tasks
MAX_REVIEW_CARDS = 10


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



SNOOZE_OPTIONS = [(15, "+15 мин"), (60, "+1 ч"), (24 * 60, "Завтра")]
SNOOZE_LABELS = {15: "+15 мин", 30: "+30 мин", 60: "+1 ч", 120: "+2 ч", 180: "+3 ч", 24 * 60: "Завтра"}


def snooze_options(usual: int | None) -> list[tuple[int, str]]:
    """Три варианта «отложить»; привычный пользователю — первым (добавляется, если его нет среди стандартных)."""
    if usual is None or usual not in SNOOZE_LABELS:
        return SNOOZE_OPTIONS
    rest = [o for o in SNOOZE_OPTIONS if o[0] != usual]
    return [(usual, SNOOZE_LABELS[usual])] + rest[:2]


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
    if digest.dates:
        parts.append("\n<b>🎂 Скоро:</b>")
        parts += [f"• {WEEKDAYS_RU[u.occurs.weekday()]} {u.occurs:%d.%m} — {html.escape(u.memory.content)}"
                  f" ({days_until_text(u.days)})" for u in digest.dates]
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


def review_kb(task: Task) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано", callback_data=TaskAction(action="done", task_id=task.id))
    kb.button(text="➡️ На завтра", callback_data=TaskAction(action="tomorrow", task_id=task.id))
    kb.button(text="🗑", callback_data=TaskAction(action="cancel", task_id=task.id))
    kb.adjust(3)
    return kb.as_markup()


def _tasks_word(n: int) -> str:
    return f"{n} {plural_ru(n, 'дело', 'дела', 'дел')}"


def review_messages(review: Review, tz: str) -> list[tuple[str, InlineKeyboardMarkup | None]]:
    """Вечерний разбор: заголовок (с «перенести всё», если дел несколько) и карточка на каждое дело."""
    done = f" Сделано за день: {_tasks_word(review.done_today)} 💪" if review.done_today else ""
    if not review.pending:
        return [(f"🌙 Все дела на сегодня закрыты 🎉{done}", None)]
    header = [f"🌙 <b>Вечерний разбор</b>\nНе закрыто: {_tasks_word(len(review.pending))}.{done}\nЧто с ними?"]
    if len(review.pending) > MAX_REVIEW_CARDS:
        header.append(f"Показываю первые {MAX_REVIEW_CARDS}, остальные — в /tasks.")
    kb = None
    if len(review.pending) > 1:
        builder = InlineKeyboardBuilder()
        builder.button(text="➡️ Перенести всё на завтра", callback_data=ReviewAction(action="all_tomorrow"))
        kb = builder.as_markup()
    messages = [("\n".join(header), kb)]
    messages += [(task_card(t, tz), review_kb(t)) for t in review.pending[:MAX_REVIEW_CARDS]]
    return messages


def _need_more(have: int, need: int) -> str:
    left = need - have
    return f"пока не понял — нужно ещё {left} {plural_ru(left, 'наблюдение', 'наблюдения', 'наблюдений')}"


def habits_text(profile: TimeProfile) -> str:
    """/habits: что бот понял о времени пользователя и как это использует."""
    lines = [f"⏱ <b>Твои привычки по времени</b> (за последние {WINDOW_DAYS} дней)\n"]
    for weekend, title in ((False, "Будни"), (True, "Выходные")):
        hour = profile.preferred_hour(weekend)
        lines.append(f"• {title}: " + (f"дела обычно делаешь около <b>{hour}:00</b>" if hour is not None
                                        else _need_more(profile.samples(weekend), MIN_HOUR_SAMPLES)))
    if bad := profile.bad_hours:
        hours = ", ".join(f"{h}:00" for h in bad)
        lines.append(f"• Глухие часы: на напоминания в <b>{hours}</b> почти не реагируешь — туда не ставлю")
    if (snooze := profile.usual_snooze) is not None:
        label = "на завтра" if snooze >= 24 * 60 else f"на {SNOOZE_LABELS[snooze].lstrip('+')}"
        lines.append(f"• Откладываешь обычно <b>{label}</b> — эта кнопка в напоминаниях стоит первой")
    else:
        lines.append("• Как откладываешь: " + _need_more(max(profile.snoozes.values(), default=0), MIN_SNOOZE_SAMPLES))
    lines.append("\nКак использую: если время не названо, ставлю дело на твой обычный час и обхожу часы, "
                 "когда ты не отвечаешь на напоминания.")
    lines.append(f"Учусь по твоим действиям: напоминаний {profile.reminders_sent}, отложено "
                 f"{sum(profile.snoozes.values())}, сделано и назначено тобой — "
                 f"{profile.samples(False) + profile.samples(True)}.")
    return "\n".join(lines)


def reminder_kb(task: Task, usual_snooze: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано", callback_data=TaskAction(action="done", task_id=task.id))
    for minutes, label in snooze_options(usual_snooze):
        kb.button(text=label, callback_data=TaskAction(action="snooze", task_id=task.id, minutes=minutes))
    kb.adjust(1, 3)
    return kb.as_markup()
