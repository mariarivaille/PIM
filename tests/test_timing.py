from datetime import timedelta
from types import SimpleNamespace

from pim.agent.tools import ToolContext, run_tool
from pim.core.models import TaskEventKind, utcnow
from pim.core.timeutils import parse_local, to_local
from pim.learning.timing import build_profile, load_profile, snooze_bucket
from pim.reminders.scheduler import ReminderScheduler
from pim.telegram.keyboards import reminder_kb, snooze_options

from tests.conftest import TZ

MONDAY = parse_local("2026-11-09T00:00", TZ)  # понедельник
SATURDAY = parse_local("2026-11-14T00:00", TZ)


def ev(kind, at, task_id=None, minutes=None):
    return SimpleNamespace(kind=kind, at=at, task_id=task_id, minutes=minutes)


async def test_lifecycle_is_logged(repo, user, monkeypatch):
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "create_task", {"title": "сам назначил", "day": "tomorrow", "time": "19:00"})
    await run_tool(ctx, "create_task", {"title": "время выбрал бот", "day": "tomorrow", "time": "11:00",
                                         "time_guessed": True})
    await run_tool(ctx, "create_task", {"title": "срок прошёл", "due_at": "2001-01-01T10:00"})

    due = await repo.create_task(user.id, "напомнить", due_at=utcnow() - timedelta(minutes=1))

    class Notifier:
        async def send_reminder(self, u, t, usual_snooze=None):
            pass

    await ReminderScheduler(repo, Notifier(), 30).tick()
    await repo.snooze_task(user.id, due.id, utcnow() + timedelta(hours=1))
    await repo.complete_task(user.id, due.id, TZ)
    await repo.cancel_task(user.id, ctx.touched[0].id)

    events = await repo.task_events(user.id, utcnow() - timedelta(days=1))
    by_kind = {k: [e for e in events if e.kind == k] for k in TaskEventKind}
    # «сам назначил» — только явное время пользователя (не «бот выбрал» и не прошедший срок), at — на когда
    [planned] = by_kind[TaskEventKind.PLANNED]
    assert planned.at == ctx.touched[0].due_at
    assert len(by_kind[TaskEventKind.REMINDED]) == 1  # прошедший срок при создании — не напоминание
    [snoozed] = by_kind[TaskEventKind.SNOOZED]
    assert 58 <= snoozed.minutes <= 60  # отложил на час
    [done] = by_kind[TaskEventKind.DONE]
    assert done.minutes is None  # после «отложить» ждали нового напоминания — реакцию не считаем
    assert len(by_kind[TaskEventKind.CANCELLED]) == 1


async def test_reaction_minutes_after_reminder(repo, user):
    task = await repo.create_task(user.id, "x", due_at=utcnow() - timedelta(minutes=1))
    await repo.mark_reminded(task.id, utcnow() - timedelta(minutes=5))
    await repo.complete_task(user.id, task.id, TZ)
    [done] = [e for e in await repo.task_events(user.id, utcnow() - timedelta(days=1)) if e.kind == TaskEventKind.DONE]
    assert done.minutes == 5


def test_profile_learns_hours_bad_times_and_snooze():
    events = []
    for d in range(5):  # будни: делает около 19
        events.append(ev(TaskEventKind.DONE, MONDAY + timedelta(days=d % 5, hours=19, minutes=10)))
    for w in range(5):  # выходные: около 12
        events.append(ev(TaskEventKind.PLANNED, SATURDAY + timedelta(days=7 * w, hours=12)))
    for i in range(3):  # напоминания в 8 утра игнорирует
        events.append(ev(TaskEventKind.REMINDED, MONDAY + timedelta(days=i, hours=8), task_id=100 + i))
    for i in range(3):  # в 18 — откладывает (это реакция)
        at = MONDAY + timedelta(days=i, hours=18)
        events += [ev(TaskEventKind.REMINDED, at, task_id=200 + i),
                   ev(TaskEventKind.SNOOZED, at + timedelta(minutes=2), task_id=200 + i, minutes=58 + i)]
    profile = build_profile(sorted(events, key=lambda e: e.at), TZ)

    assert profile.preferred_hour(weekend=False) == 19
    assert profile.preferred_hour(weekend=True) == 12
    assert profile.bad_hours == [8]
    assert profile.usual_snooze == 60
    assert profile.prompt_lines() == [
        "Обычно делает дела и назначает их в будни около 19:00, в выходные около 12:00.",
        "На напоминания в 8:00 почти не реагирует — не ставь на это время.",
        "Чаще всего откладывает напоминания на 1 ч.",
    ]


def test_profile_needs_enough_data():
    events = [ev(TaskEventKind.DONE, MONDAY + timedelta(hours=19)) for _ in range(4)]
    events += [ev(TaskEventKind.SNOOZED, MONDAY, minutes=60) for _ in range(2)]
    profile = build_profile(events, TZ)
    assert profile.preferred_hour(False) is None and profile.usual_snooze is None and profile.prompt_lines() == []


def test_preferred_hour_skips_night_and_bad_hours():
    events = [ev(TaskEventKind.DONE, MONDAY + timedelta(hours=23)) for _ in range(6)]
    events += [ev(TaskEventKind.DONE, MONDAY + timedelta(hours=20)) for _ in range(2)]
    assert build_profile(events, TZ).preferred_hour(False) == 20  # 23:00 — ночь, не предлагаем


def test_snooze_bucket():
    assert [snooze_bucket(m) for m in (10, 25, 70, 110, 200, 23 * 60)] == [15, 30, 60, 120, 180, 1440]


async def test_default_time_follows_habits(repo, user):
    for d in range(5):
        await repo.log_event(user.id, TaskEventKind.DONE, MONDAY + timedelta(days=d, hours=18, minutes=30))
    for w in range(5):
        await repo.log_event(user.id, TaskEventKind.DONE, SATURDAY - timedelta(days=7 * w, hours=-13))
    profile = await load_profile(repo, user, now=SATURDAY + timedelta(days=1))
    ctx = ToolContext(repo, user, profile=profile, asked_for_time=True)  # модель время не выбрала

    await run_tool(ctx, "create_task", {"title": "будни", "due_at": "2030-01-10"})   # четверг
    await run_tool(ctx, "create_task", {"title": "выходной", "due_at": "2030-01-12"})  # суббота
    await run_tool(ctx, "create_task", {"title": "день недели", "day": "fri"})
    hours = [to_local(t.due_at, TZ).hour for t in ctx.touched]
    assert hours == [18, 13, 18]


def test_snooze_buttons_follow_habit():
    assert snooze_options(None)[0] == (15, "+15 мин")
    assert snooze_options(60) == [(60, "+1 ч"), (15, "+15 мин"), (24 * 60, "Завтра")]
    assert snooze_options(120) == [(120, "+2 ч"), (15, "+15 мин"), (60, "+1 ч")]
    kb = reminder_kb(SimpleNamespace(id=1), usual_snooze=120)
    assert [b.text for b in kb.inline_keyboard[1]] == ["+2 ч", "+15 мин", "+1 ч"]
