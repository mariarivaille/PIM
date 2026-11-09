from datetime import date, timedelta

import pytest

from pim.core.models import MemoryKind
from pim.core.timeutils import parse_local, to_local
from pim.core.yearly import YearlyDate, next_occurrence, parse_yearly_date
from pim.reminders.dates import CONGRATS_OFFSET, GIFT_OFFSET, reminder_at, upcoming_dates
from pim.reminders.digest import build_digest
from pim.reminders.scheduler import ReminderScheduler
from pim.telegram.keyboards import DATE_GIFT, date_alert_kb, date_alert_text, format_digest

from tests.conftest import TZ


@pytest.mark.parametrize("text, expected", [
    ("Мама — Ольга, ДР 14 марта", YearlyDate(3, 14)),
    ("У моего папы день рождения 22 марта.", YearlyDate(3, 22)),
    ("Брат Савва родился 3 мая 1998", YearlyDate(5, 3, 1998)),
    ("Годовщина свадьбы 02.08.2015", YearlyDate(8, 2, 2015)),
    ("юбилей мамы 9 мая", YearlyDate(5, 9)),
    ("др Андрея 29.02", YearlyDate(2, 29)),
    ("в отпуске до 12 ноября", None),         # нет признака ежегодной даты
    ("ДР бабушки 31 февраля", None),          # такой даты нет
    ("Андрей — парень", None),
])
def test_parse_yearly_date(text, expected):
    assert parse_yearly_date(text) == expected


def test_next_occurrence():
    assert next_occurrence(3, 14, date(2026, 11, 10)) == date(2027, 3, 14)
    assert next_occurrence(11, 10, date(2026, 11, 10)) == date(2026, 11, 10)  # сегодня тоже считается
    assert next_occurrence(2, 29, date(2026, 3, 1)) == date(2027, 2, 28)      # невисокосный год


async def test_dates_are_parsed_on_save_update_and_backfill(repo, user):
    m = await repo.add_memory(user.id, "Мама — Ольга, ДР 12 марта", MemoryKind.PERSON)
    assert (m.date_month, m.date_day) == (3, 12)
    m = await repo.update_memory(user.id, m.number, content="Мама — Ольга, ДР 14 марта")
    assert (m.date_month, m.date_day) == (3, 14)
    m = await repo.update_memory(user.id, m.number, content="Мама — Ольга")
    assert m.date_month is None

    # старая запись без дат — подхватывается при старте
    old = await repo.add_memory(user.id, "Папа, день рождения 22 марта")
    await repo.update_memory(user.id, old.number, date_month=None, date_day=None)
    assert await repo.backfill_memory_dates() == 1
    assert [x.content for x in await repo.memories_with_dates(user.id)] == ["Папа, день рождения 22 марта"]


async def test_upcoming_dates_and_age(repo, user):
    await repo.add_memory(user.id, "Брат Савва родился 17 ноября 1998")
    await repo.add_memory(user.id, "Мама — Ольга, ДР 11 ноября")
    await repo.add_memory(user.id, "Папа — ДР 22 марта")
    ups = upcoming_dates(await repo.memories_with_dates(user.id), date(2026, 11, 10), horizon_days=7)
    assert [(u.memory.content[:4], u.days, u.age) for u in ups] == [("Мама", 1, None), ("Брат", 7, 28)]


async def test_scheduler_alerts_at_7_1_0_days_once(repo, user, monkeypatch):
    await repo.add_memory(user.id, "Мама — Ольга, ДР 17 ноября")
    sent = []

    class Notifier:
        async def send_date_alert(self, u, up):
            sent.append(up.days)

    sched = ReminderScheduler(repo, Notifier(), 30)
    for day, expected in [(9, []), (10, [7]), (11, []), (16, [1]), (17, [0])]:
        now = parse_local(f"2026-11-{day:02d}T09:05", TZ)  # время утренней сводки
        monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda now=now: now)
        sent.clear()
        await sched.tick_dates()
        await sched.tick_dates()  # повторная проверка в тот же день ничего не шлёт
        assert sent == expected, day


async def test_dates_check_without_digest_runs_at_10(repo, user, monkeypatch):
    await repo.add_memory(user.id, "Мама — Ольга, ДР 17 ноября")
    user_row = await repo.get_user(user.id)
    await repo.set_digest_time(user_row.id, None)
    sent = []

    class Notifier:
        async def send_date_alert(self, u, up):
            sent.append(up.days)

    for hhmm, expected in [("09:05", []), ("10:05", [7])]:
        now = parse_local(f"2026-11-10T{hhmm}", TZ)
        monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda now=now: now)
        await ReminderScheduler(repo, Notifier(), 30).tick_dates()
        assert sent == expected


async def test_deleting_memory_cleans_alerts(repo, user):
    m = await repo.add_memory(user.id, "Мама — Ольга, ДР 17 ноября")
    await repo.record_date_alert(m.id, "2026-11-17", 7)
    await repo.delete_memory(user.id, m.number)
    assert not await repo.date_alert_sent(m.id, "2026-11-17", 7)


async def test_digest_shows_upcoming_dates(repo, user):
    await repo.add_memory(user.id, "Мама — Ольга, ДР 14 ноября")
    digest = await build_digest(repo, user, parse_local("2026-11-10T09:00", TZ))
    text = format_digest(digest, TZ)
    assert "🎂 Скоро" in text and "сб 14.11 — Мама — Ольга, ДР 14 ноября (через 4 дня)" in text


async def test_alert_text_and_buttons(repo, user):
    m = await repo.add_memory(user.id, "Брат Савва, ДР 17 ноября 1998")
    [up] = upcoming_dates([m], date(2026, 11, 10), 7)
    assert date_alert_text(up).startswith("🎂 <b>Через 7 дней, вт 17.11</b>") and "исполнится 28" in date_alert_text(up)
    assert [r[0].text for r in date_alert_kb(up).inline_keyboard] == [
        "🎁 Напомнить купить подарок", "📞 Напомнить поздравить", "👌 Не нужно"]
    assert [r[0].text for r in date_alert_kb(up, done=DATE_GIFT).inline_keyboard] == [
        "📞 Напомнить поздравить", "👌 Не нужно"]
    [today] = upcoming_dates([m], date(2026, 11, 17), 7)
    assert date_alert_text(today).startswith("🎂 <b>Сегодня</b>") and date_alert_kb(today) is None


def test_reminder_times():
    now = parse_local("2026-11-10T09:05", TZ)
    gift = reminder_at(date(2026, 11, 17), GIFT_OFFSET, now, TZ)
    assert to_local(gift, TZ).strftime("%d.%m %H:%M") == "15.11 19:00"
    congrats = reminder_at(date(2026, 11, 17), CONGRATS_OFFSET, now, TZ)
    assert to_local(congrats, TZ).strftime("%d.%m %H:%M") == "17.11 10:00"
    # время подарка уже прошло (дата завтра) — ближайшее разумное, не в прошлом
    late = reminder_at(date(2026, 11, 11), GIFT_OFFSET, now, TZ)
    assert late > now and late - now < timedelta(hours=3)
