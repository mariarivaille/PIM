from datetime import datetime, timedelta, timezone

from aid.core.models import TaskStatus, utcnow
from aid.core.timeutils import parse_local, to_local

from tests.conftest import TZ


async def test_due_and_mark_reminded(repo, user):
    past = utcnow() - timedelta(minutes=1)
    future = utcnow() + timedelta(hours=1)
    t1 = await repo.create_task(user.id, "past", due_at=past)
    await repo.create_task(user.id, "future", due_at=future)
    await repo.create_task(user.id, "no due")

    due = await repo.due_tasks(utcnow())
    assert [t.id for t, _ in due] == [t1.id]
    assert due[0][0].due_at.tzinfo is not None

    await repo.mark_reminded(t1.id, utcnow())
    assert await repo.due_tasks(utcnow()) == []


async def test_snooze_resets_reminded(repo, user):
    t = await repo.create_task(user.id, "x", due_at=utcnow() - timedelta(minutes=1))
    await repo.mark_reminded(t.id, utcnow())
    await repo.snooze_task(user.id, t.id, utcnow() - timedelta(seconds=1))
    assert len(await repo.due_tasks(utcnow())) == 1


async def test_complete_recurring_moves_to_next(repo, user):
    first = parse_local("2026-10-27T09:00", TZ)  # вторник
    t = await repo.create_task(user.id, "gym", due_at=first, rrule="FREQ=WEEKLY;BYDAY=TU,TH")
    done = await repo.complete_task(user.id, t.id, TZ)
    assert done.status == TaskStatus.OPEN
    assert done.due_at > utcnow()
    local = to_local(done.due_at, TZ)
    assert local.weekday() in (1, 3)  # вт / чт
    assert (local.hour, local.minute) == (9, 0)


async def test_complete_plain_task(repo, user):
    t = await repo.create_task(user.id, "x")
    done = await repo.complete_task(user.id, t.id, TZ)
    assert done.status == TaskStatus.DONE
    assert await repo.list_open_tasks(user.id) == []
    assert await repo.complete_task(user.id, t.id, TZ) is None


async def test_other_user_cannot_touch(repo, user):
    t = await repo.create_task(user.id, "mine")
    await repo.get_or_create_user(2, 200, TZ)
    assert await repo.get_task(2, t.id) is None
    assert await repo.cancel_task(2, t.id) is None


def test_parse_local_naive_is_user_tz():
    dt = parse_local("2026-10-30T15:00", TZ)
    assert dt == datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
