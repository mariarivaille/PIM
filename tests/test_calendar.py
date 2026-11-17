from datetime import timedelta

from pim.agent.tools import ToolContext, run_tool
from pim.core import calendar as cal
from pim.core.timeutils import parse_local, to_local
from pim.reminders.digest import build_digest
from pim.reminders.review import evening_review
from pim.reminders.scheduler import ReminderScheduler
from pim.telegram.keyboards import calendar_text, event_card, format_digest

from tests.conftest import TZ

MON = parse_local("2030-11-18T09:00", TZ)  # понедельник


def hm(dt):
    return to_local(dt, TZ).strftime("%a %d.%m %H:%M")


async def gym(repo, user):
    """Спортзал по понедельникам 19–21, напоминать за 30 мин."""
    return await repo.create_event(user.id, title="спортзал", starts_at=parse_local("2030-11-18T19:00", TZ),
                                   ends_at=parse_local("2030-11-18T21:00", TZ), rrule="FREQ=WEEKLY;BYDAY=MO",
                                   remind_before=30)


async def test_recurring_occurrences_and_busy(repo, user):
    event = await gym(repo, user)
    occ = cal.occurrences(event, MON, MON + timedelta(days=14), TZ)
    assert [hm(o.start) for o in occ] == ["Mon 18.11 19:00", "Mon 25.11 19:00"]
    events = [event]
    assert cal.conflicts(events, parse_local("2030-11-25T20:00", TZ), TZ)
    assert not cal.conflicts(events, parse_local("2030-11-25T21:00", TZ), TZ)  # конец — уже свободно
    assert hm(cal.next_free(events, parse_local("2030-11-25T19:30", TZ), TZ)) == "Mon 25.11 21:00"
    # напоминание о следующем разе — за 30 мин
    assert hm(cal.next_reminder(event, MON, TZ, MON)) == "Mon 18.11 18:30"
    assert hm(cal.next_reminder(event, parse_local("2030-11-18T19:00", TZ), TZ, MON)) == "Mon 25.11 18:30"


async def test_create_event_tool(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "create_event", {"title": "маникюр", "date": "2030-11-23", "start": "12:00",
                                               "minutes": 90})
    assert "дороги" in res["error"]  # выход из дома — сначала подумать о запасе на дорогу
    res = await run_tool(ctx, "create_event", {"title": "маникюр", "date": "2030-11-23", "start": "12:00",
                                               "minutes": 90, "remind_before_minutes": 45, "location": "Ленина 5"})
    ev = res["created_event"]
    assert ev["when"] == "сб 23.11 12:00–13:30 Маникюр" and ev["next_reminder"].endswith("11:15")
    again = await run_tool(ToolContext(repo, user), "create_event",
                           {"title": "Маникюр", "date": "2030-11-23", "start": "12:00", "remind_before_minutes": 45})
    assert "already_exists" in again
    # без длительности — час; «до» раньше «с» — через полночь
    res = await run_tool(ctx, "create_event", {"title": "созвон", "date": "2030-11-23", "start": "12:30",
                                               "remind_before_minutes": 0})
    assert res["created_event"]["when"].endswith("12:30–13:30 Созвон") and res["overlaps"]  # наложился на маникюр
    res = await run_tool(ctx, "create_event", {"title": "смена", "date": "2030-11-24", "start": "22:00",
                                               "end": "02:00", "remind_before_minutes": 0})
    assert "пн 25.11 02:00" in res["created_event"]["when"]
    res = await run_tool(ctx, "create_event", {"title": "отпуск", "date": "2030-12-01", "all_day": True, "days": 10})
    assert res["created_event"]["when"] == "весь день вс 01.12–10.12 Отпуск"


async def test_update_and_cancel_event(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "create_event", {"title": "встреча с Олей", "date": "2030-11-21", "start": "14:00",
                                               "minutes": 90, "remind_before_minutes": 30})
    eid = res["created_event"]["id"]
    # «перенеси на пятницу» — то же время и длительность
    res = await run_tool(ctx, "update_event", {"event_id": eid, "day": "fri"})
    when = res["updated_event"]["when"]
    assert when.startswith("пт ") and "14:00–15:30" in when
    res = await run_tool(ctx, "update_event", {"event_id": eid, "start": "16:00"})
    assert "16:00–17:30" in res["updated_event"]["when"] and res["updated_event"]["next_reminder"].endswith("15:30")
    assert (await run_tool(ctx, "cancel_event", {"event_id": eid}))["cancelled_event"] == "Встреча с Олей"
    assert await repo.list_events(user.id) == []


async def test_tasks_avoid_events(repo, user):
    await gym(repo, user)
    ctx = ToolContext(repo, user, asked_for_time=True)
    # время выбирал бот — мимо спортзала
    res = await run_tool(ctx, "create_task", {"title": "разобрать бумаги", "date_from_forwarded": True,
                                              "due_at": "2030-11-25T19:30", "time_guessed": True})
    assert res["created"]["due"].endswith("21:00") and "conflicts_with" not in res["created"]
    # время назвал пользователь — ставим, но предупреждаем
    res = await run_tool(ctx, "create_task", {"title": "позвонить маме", "due_at": "2030-11-25T19:30"})
    assert res["created"]["due"].endswith("19:30") and "спортзал" in res["created"]["conflicts_with"][0]


async def test_event_reminders_tick(repo, user, monkeypatch):
    event = await gym(repo, user)
    await repo.update_event(user.id, event.id, next_reminder_at=cal.next_reminder(event, MON, TZ, MON))
    sent = []

    class Notifier:
        async def send_event_reminder(self, u, occ):
            sent.append(hm(occ.start))

    sched = ReminderScheduler(repo, Notifier(), 30)
    monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda: parse_local("2030-11-18T18:29", TZ))
    assert await sched.tick_events() == 0
    monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda: parse_local("2030-11-18T18:30", TZ))
    assert await sched.tick_events() == 1 and await sched.tick_events() == 0
    assert sent == ["Mon 18.11 19:00"]
    nxt = (await repo.list_events(user.id))[0].next_reminder_at
    assert hm(nxt) == "Mon 25.11 18:30"  # следующий понедельник


async def test_digest_calendar_and_review(repo, user):
    await gym(repo, user)
    digest = await build_digest(repo, user, MON)
    assert [o.event.title for o in digest.events] == ["спортзал"]
    assert "19:00–21:00 спортзал" in format_digest(digest, TZ)
    # вечерний разбор переносит на завтра мимо встреч: дело на вс 19:30 → пн 19:30 занято → 21:00
    await repo.create_task(user.id, "отчёт", due_at=parse_local("2030-11-17T19:30", TZ))
    review = await evening_review(repo, user, parse_local("2030-11-17T21:00", TZ))
    assert hm(review.moved[0].due_at) == "Mon 18.11 21:00"


async def test_calendar_text(repo, user):
    event = await gym(repo, user)
    trip = await repo.create_event(user.id, title="Отпуск", starts_at=parse_local("2031-01-01T00:00", TZ),
                                   ends_at=parse_local("2031-01-11T00:00", TZ), all_day=True)
    nails = await repo.create_event(user.id, title="Маникюр", starts_at=parse_local("2030-11-26T19:00", TZ),
                                    ends_at=parse_local("2030-11-26T20:30", TZ))
    past = await repo.create_event(user.id, title="Было", starts_at=parse_local("2030-11-01T19:00", TZ),
                                   ends_at=parse_local("2030-11-01T20:00", TZ))
    text = calendar_text([event, trip, nails, past], TZ, MON)
    assert "🔁 по пн, 19:00–21:00 Спортзал" in text.replace("спортзал", "Спортзал")
    assert text.index("вт 26.11") < text.index("ср 01.01")  # все будущие, по порядку — даже через полтора месяца
    assert "ср 01.01 – пт 10.01</b> · 10 дней" in text and "🌴 Отпуск" in text and "Было" not in text
    assert "🔔 напомню" not in event_card(event, TZ)  # next_reminder ещё не посчитан


async def test_all_day_reminder_is_not_at_night(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "create_event", {"title": "отпуск", "date": "2030-12-01", "all_day": True, "days": 10,
                                               "remind_before_minutes": 1440})
    assert res["created_event"]["next_reminder"] == "сб 30.11 10:00"


async def test_shift_off_busy_time_is_reported(repo, user):
    await gym(repo, user)
    ctx = ToolContext(repo, user, asked_for_time=True)
    res = await run_tool(ctx, "create_task", {"title": "разобрать бумаги", "due_at": "2030-11-25T19:00",
                                              "time_guessed": True})
    assert "21:00" in res["created"]["moved_off_busy_time"]


async def test_my_tasks_has_no_events(repo, user):
    from pim.telegram.keyboards import tasks_text
    assert tasks_text([], TZ) == "Открытых дел нет 🎉"  # события — только в календаре


def test_rule_text_until_and_count():
    from datetime import datetime, timezone

    from pim.core.calendar import rule_text
    start = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
    assert rule_text("FREQ=DAILY;UNTIL=20261127T190000", start, TZ) == "каждый день до 27.11"
    assert rule_text("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4", start, TZ) == "по пн, ср, 4 раза"
