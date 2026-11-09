from datetime import timedelta

from pim.agent.tools import ToolContext, run_tool
from pim.core.timeutils import parse_local, to_local
from pim.reminders.review import (
    build_review,
    postpone_all_to_tomorrow,
    postpone_to_tomorrow,
    review_due,
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
            sent.append(len(review.pending))

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


async def test_review_messages(repo, user):
    for i in range(MAX_REVIEW_CARDS + 2):
        await repo.create_task(user.id, f"дело {i}", due_at=parse_local("2026-11-10T10:00", TZ) + timedelta(minutes=i))
    msgs = review_messages(await build_review(repo, user, EVENING), TZ)
    header, header_kb = msgs[0]
    assert "Не закрыто: 12 дел" in header and "первые 10" in header
    assert header_kb.inline_keyboard[0][0].text == "➡️ Перенести всё на завтра"
    assert len(msgs) == 1 + MAX_REVIEW_CARDS
    assert [b.text for b in msgs[1][1].inline_keyboard[0]] == ["✅ Сделано", "➡️ На завтра", "🗑"]


def test_all_done_message():
    from pim.reminders.review import Review
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
