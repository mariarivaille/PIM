from datetime import timedelta

from sqlalchemy import text

from aid.agent.tools import ToolContext, run_tool
from aid.core.db import init_db, make_engine, make_sessionmaker
from aid.core.repository import Repository
from aid.core.timeutils import parse_local
from aid.reminders.digest import build_digest, digest_due
from aid.reminders.scheduler import ReminderScheduler
from aid.telegram.keyboards import format_digest

from tests.conftest import TZ

NOW = parse_local("2026-11-10T09:05", TZ)


async def test_build_digest_sections(repo, user):
    await repo.create_task(user.id, "старое", due_at=parse_local("2026-11-08T10:00", TZ))
    await repo.create_task(user.id, "утро", due_at=parse_local("2026-11-10T08:00", TZ))
    await repo.create_task(user.id, "вечер", due_at=parse_local("2026-11-10T23:30", TZ))
    await repo.create_task(user.id, "завтра", due_at=parse_local("2026-11-11T00:10", TZ))
    await repo.create_task(user.id, "когда-нибудь")

    d = await build_digest(repo, user, NOW)
    assert [t.title for t in d.overdue] == ["старое"]
    assert [t.title for t in d.today] == ["утро", "вечер"]
    assert [t.title for t in d.upcoming] == ["завтра"]
    assert [t.title for t in d.undated] == ["когда-нибудь"]

    rendered = format_digest(d, TZ)
    assert "вт 10.11" in rendered and "23:30 — вечер" in rendered
    assert "ср 11.11 00:10 — завтра" in rendered


async def test_long_digest_is_truncated(repo, user):
    for i in range(300):
        await repo.create_task(user.id, f"дело номер {i} с довольно длинным названием")
    rendered = format_digest(await build_digest(repo, user, NOW), TZ)
    assert len(rendered) <= 4096
    assert rendered.endswith("не влезло в сообщение")


async def test_empty_digest(repo, user):
    d = await build_digest(repo, user, NOW)
    assert d.is_empty
    assert "Дел нет" in format_digest(d, TZ)


async def test_digest_due_window(user):
    user.digest_time = "09:00"
    assert not digest_due(user, NOW - timedelta(minutes=10))
    assert digest_due(user, NOW)
    assert not digest_due(user, NOW + timedelta(hours=3))  # опоздали — не догоняем
    user.last_digest_on = "2026-11-10"
    assert not digest_due(user, NOW)
    user.digest_time = None
    user.last_digest_on = None
    assert not digest_due(user, NOW)


async def test_scheduler_sends_digest_once_per_day(repo, user, monkeypatch):
    sent = []

    class FakeNotifier:
        async def send_digest(self, u, d):
            sent.append(u.id)

    monkeypatch.setattr("aid.reminders.scheduler.utcnow", lambda: NOW)
    sched = ReminderScheduler(repo, FakeNotifier(), 30)
    assert await sched.tick_digests() == 1
    assert await sched.tick_digests() == 0
    assert sent == [user.id]
    assert (await repo.get_user(user.id)).last_digest_on == "2026-11-10"


async def test_set_digest_time_tool(repo, user):
    ctx = ToolContext(repo, user)
    assert (await run_tool(ctx, "set_digest_time", {"time": "8:30"}))["digest_time"] == "08:30"
    assert (await repo.get_user(user.id)).digest_time == "08:30"
    assert "error" in await run_tool(ctx, "set_digest_time", {"time": "утром"})
    assert (await run_tool(ctx, "set_digest_time", {"time": "off"}))["digest"] == "off"
    assert [u.id for u in await repo.users_with_digest()] == []


async def test_migration_adds_columns_to_old_db(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    async with engine.begin() as conn:  # схема из первой версии
        await conn.execute(text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY, chat_id BIGINT, timezone VARCHAR(64), created_at DATETIME)"
        ))
        await conn.execute(text("INSERT INTO users VALUES (1, 100, 'Europe/Moscow', '2026-11-01 00:00:00')"))
    await init_db(engine)

    repo = Repository(make_sessionmaker(engine))
    user = await repo.get_user(1)
    assert user.digest_time == "09:00"  # существующим пользователям сводка включается
    assert user.last_digest_on is None
    await engine.dispose()
