from datetime import time, timedelta

import pytest

from pim.agent.tools import ToolContext, run_tool
from pim.core.models import utcnow
from pim.core.timeutils import parse_local, resolve_day_time, to_local

from tests.conftest import TZ

MONDAY_EVENING = parse_local("2026-11-09T22:40", TZ)


@pytest.mark.parametrize("day, expected", [
    ("today", "2026-11-09"),
    ("tomorrow", "2026-11-10"),
    ("day_after_tomorrow", "2026-11-11"),
    ("thu", "2026-11-12"),
    ("fri", "2026-11-13"),
    ("sat", "2026-11-14"),
    ("sun", "2026-11-15"),
    ("mon", "2026-11-16"),  # сегодняшний день недели — следующая неделя
])
def test_weekdays(day, expected):
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, day, time(19, 0)), TZ)
    assert dt.date().isoformat() == expected
    assert (dt.hour, dt.minute) == (19, 0)


def test_time_only_in_past_moves_to_tomorrow():
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, None, time(9, 0)), TZ)
    assert dt.date().isoformat() == "2026-11-10"
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, None, time(23, 30)), TZ)
    assert dt.date().isoformat() == "2026-11-09"


def test_day_without_time_defaults_to_10():
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, "fri", None), TZ)
    assert (dt.hour, dt.minute) == (10, 0)


async def test_tool_in_minutes(repo, user):
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "create_task", {"title": "x", "in_minutes": 120})
    task = ctx.touched[0]
    assert abs(task.due_at - (utcnow() + timedelta(minutes=120))) < timedelta(seconds=5)


async def test_tool_validates_day_and_time(repo, user):
    ctx = ToolContext(repo, user)
    assert "error" in await run_tool(ctx, "create_task", {"title": "x", "day": "пятница"})
    assert "error" in await run_tool(ctx, "create_task", {"title": "x", "day": "fri", "time": "вечером"})


async def test_update_time_only_keeps_day(repo, user):
    ctx = ToolContext(repo, user)
    task = await repo.create_task(user.id, "банк", due_at=parse_local("2030-01-10T10:00", TZ))
    await run_tool(ctx, "update_task", {"task_id": task.id, "time": "12:00"})
    updated = to_local((await repo.get_task(user.id, task.id)).due_at, TZ)
    assert updated.isoformat().startswith("2030-01-10T12:00")
