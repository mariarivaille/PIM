from datetime import timedelta

from pim.agent.tools import ToolContext, run_tool
from pim.core.timeutils import parse_local, to_local
from pim.reminders.review import (
    Review,
    build_review,
    evening_review,
    postpone_all_to_tomorrow,
    postpone_to_tomorrow,
    review_due,
    roll_over,
    tomorrow_same_time,
)
from pim.reminders.scheduler import ReminderScheduler
from pim.telegram.keyboards import MAX_REVIEW_CARDS, review_messages

from tests.conftest import TZ

EVENING = parse_local("2026-11-10T21:05", TZ)


async def test_review_picks_today_and_overdue(repo, user):
    await repo.create_task(user.id, "просрочено", due_at=parse_local("2026-11-08T10:00", TZ))
    await repo.create_task(user.id, "сегодня", due_at=parse_local("2026-11-10T15:00", TZ))
    await repo.create_task(user.id, "позже сегодня", due_at=parse_local("2026-11-10T23:30", TZ))
    await repo.create_task(user.id, "завтра", due_at=parse_local("2026-11-11T10:00", TZ))
    await repo.create_task(user.id, "без срока")
    done = await repo.create_task(user.id, "сделала", due_at=parse_local("2026-11-10T12:00", TZ))
    await repo.update_task(user.id, done.id, status="done", completed_at=parse_local("2026-11-10T12:30", TZ))

    review = await build_review(repo, user, EVENING)
    assert [t.title for t in review.pending] == ["просрочено", "сегодня", "позже сегодня"]
    assert review.done_today == 1 and review.should_send


async def test_nothing_to_review_stays_silent(repo, user):
    await repo.create_task(user.id, "завтра", due_at=parse_local("2026-11-11T10:00", TZ))
    assert not (await build_review(repo, user, EVENING)).should_send


async def test_postpone_keeps_time_and_rings_again(repo, user):
    task = await repo.create_task(user.id, "x", due_at=parse_local("2026-11-08T10:15", TZ))
    await repo.mark_reminded(task.id, EVENING)
    moved = await postpone_to_tomorrow(repo, user, task.id, EVENING)
    assert to_local(moved.due_at, TZ).strftime("%d.%m %H:%M") == "11.11 10:15"
    assert moved.reminded_at is None  # завтра снова напомнит
    assert to_local(tomorrow_same_time(parse_local("2026-11-10T21:00", TZ), EVENING, TZ), TZ).day == 11


async def test_postpone_all(repo, user):
    for h in (9, 15):
        await repo.create_task(user.id, f"дело {h}", due_at=parse_local(f"2026-11-10T{h:02d}:00", TZ))
    other = await repo.get_or_create_user(2, 200, TZ)
    await repo.create_task(other.id, "чужое", due_at=parse_local("2026-11-10T09:00", TZ))
    moved = await postpone_all_to_tomorrow(repo, user, EVENING)
    assert sorted(to_local(t.due_at, TZ).strftime("%d.%m %H:%M") for t in moved) == ["11.11 09:00", "11.11 15:00"]
    assert (await build_review(repo, user, EVENING)).pending == []
    assert len((await build_review(repo, other, EVENING)).pending) == 1  # чужие дела не тронуты


async def test_review_schedule_once_per_day(repo, user, monkeypatch):
    user.review_time = "21:00"
    assert not review_due(user, EVENING - timedelta(minutes=10))
    assert review_due(user, EVENING)

    await repo.create_task(user.id, "x", due_at=parse_local("2026-11-10T15:00", TZ))
    sent = []

    class Notifier:
        async def send_review(self, u, review):
            sent.append(len(review.moved))

    monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda: EVENING)
    sched = ReminderScheduler(repo, Notifier(), 30)
    assert await sched.tick_reviews() == 1
    assert await sched.tick_reviews() == 0
    assert sent == [1]


async def test_review_day_marked_even_when_silent(repo, user, monkeypatch):
    class Notifier:
        async def send_review(self, u, review):
            raise AssertionError("разбирать нечего — писать не должны")

    monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda: EVENING)
    assert await ReminderScheduler(repo, Notifier(), 30).tick_reviews() == 0
    assert (await repo.get_user(user.id)).last_review_on == "2026-11-10"


async def test_evening_review_moves_overdue_to_tomorrow(repo, user):
    await repo.create_task(user.id, "просрочено", due_at=parse_local("2026-11-08T10:00", TZ))
    await repo.create_task(user.id, "сегодня", due_at=parse_local("2026-11-10T15:00", TZ))
    await repo.create_task(user.id, "позже сегодня", due_at=parse_local("2026-11-10T23:30", TZ))
    await repo.create_task(user.id, "каждый день", due_at=parse_local("2026-11-10T09:00", TZ), rrule="FREQ=DAILY")
    await repo.create_task(user.id, "по вторникам", due_at=parse_local("2026-11-10T08:00", TZ),
                           rrule="FREQ=WEEKLY;BYDAY=TU")
    review = await evening_review(repo, user, EVENING)

    moved = {t.title: to_local(t.due_at, TZ).strftime("%d.%m %H:%M") for t in review.moved}
    assert moved == {"просрочено": "11.11 10:00", "сегодня": "11.11 15:00",
                     "каждый день": "11.11 09:00", "по вторникам": "17.11 08:00"}
    assert [t.title for t in review.later] == ["позже сегодня"]
    # перенос — решение бота, а не «отложил» пользователя: умное время им не учится
    assert await repo.task_events(user.id, EVENING - timedelta(days=30)) == []
    again = await evening_review(repo, user, EVENING)  # повторный разбор ничего не двигает
    assert again.moved == [] and [t.title for t in again.later] == ["позже сегодня"]


async def test_review_is_one_message(repo, user):
    for i in range(MAX_REVIEW_CARDS + 2):
        await repo.create_task(user.id, f"дело {i}", due_at=parse_local("2026-11-10T10:00", TZ) + timedelta(minutes=i))
    await repo.create_task(user.id, "позже", due_at=parse_local("2026-11-10T23:30", TZ))
    [(text, kb)] = review_messages(await evening_review(repo, user, EVENING), TZ)
    assert "перенёс на завтра (12 дел)" in text and "1. дело 0 — ср 11.11 10:00" in text
    assert "…и ещё 2" in text and "• позже — 23:30" in text
    buttons = [b.text for row in kb.inline_keyboard for b in row]
    assert buttons == [f"✅ {i}" for i in range(1, MAX_REVIEW_CARDS + 1)]


async def test_scheduler_review_goes_to_history(repo, user, monkeypatch):
    monkeypatch.setattr("pim.reminders.scheduler.utcnow", lambda: EVENING)
    await repo.create_task(user.id, "банк", due_at=parse_local("2026-11-10T15:00", TZ))

    class Notifier:
        async def send_review(self, u, review):
            pass

    assert await ReminderScheduler(repo, Notifier(), 30).tick_reviews() == 1
    [msg] = await repo.recent_messages(user.id, 10)
    assert msg.role == "assistant" and "1. банк — ср 11.11 15:00" in msg.content


def test_all_done_message():
    [(text, kb)] = review_messages(Review(pending=[], done_today=3), TZ)
    assert "закрыты" in text and "3 дела" in text and kb is None


async def test_set_review_time_tool(repo, user):
    ctx = ToolContext(repo, user)
    assert (await run_tool(ctx, "set_review_time", {"time": "22:30"}))["review_time"] == "22:30"
    assert (await run_tool(ctx, "set_review_time", {"time": "off"}))["review_time"] == "off"
    assert await repo.users_with_review() == []


async def test_update_to_other_day_keeps_time(repo, user):
    task = await repo.create_task(user.id, "банк", due_at=parse_local("2030-01-10T16:45", TZ))
    res = await run_tool(ToolContext(repo, user), "update_task", {"task_id": task.id, "day": "fri"})
    assert res["updated"]["due"].endswith("16:45") and "time_chosen_by_assistant" not in res["updated"]


async def test_rollover_moves_past_days_to_today(repo, user):
    morning = parse_local("2026-11-11T09:00", TZ)
    await repo.create_task(user.id, "вчера вечером", due_at=parse_local("2026-11-10T19:00", TZ))
    await repo.create_task(user.id, "вчера утром", due_at=parse_local("2026-11-10T08:00", TZ))
    await repo.create_task(user.id, "сегодня рано", due_at=parse_local("2026-11-11T07:00", TZ))
    await repo.create_task(user.id, "каждый день", due_at=parse_local("2026-11-09T21:30", TZ), rrule="FREQ=DAILY")
    other = await repo.get_or_create_user(2, 200, TZ)
    await repo.create_task(other.id, "чужое позавчера", due_at=parse_local("2026-11-09T12:00", TZ))

    moved = {t.title: to_local(t.due_at, TZ).strftime("%d.%m %H:%M") for t in await roll_over(repo, morning)}
    assert moved == {
        "вчера вечером": "11.11 19:00",
        "вчера утром": "11.11 10:00",  # 08:00 сегодня уже прошло — на ближайшее разумное время
        "каждый день": "11.11 21:30",
        "чужое позавчера": "11.11 12:00",
    }  # «сегодня рано» не трогаем — его разберёт вечер
    assert await roll_over(repo, morning) == []
    assert await repo.task_events(user.id, morning - timedelta(days=30)) == []
