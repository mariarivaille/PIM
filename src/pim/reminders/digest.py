"""Утренняя сводка: собирается из БД детерминированно, без LLM."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pim.core.models import Task, User
from pim.core.repository import Repository
from pim.core.timeutils import to_local

# Если бот был выключен в момент сводки, догоняем её не позже чем через это окно
CATCH_UP_WINDOW = timedelta(hours=3)


@dataclass
class Digest:
    day: datetime  # полночь текущего локального дня
    overdue: list[Task] = field(default_factory=list)
    today: list[Task] = field(default_factory=list)
    upcoming: list[Task] = field(default_factory=list)
    undated: list[Task] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.overdue or self.today or self.upcoming or self.undated)


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


async def build_digest(repo: Repository, user: User, now: datetime) -> Digest:
    local_now = to_local(now, user.timezone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    digest = Digest(day=day_start)
    for task in await repo.list_open_tasks(user.id):
        if task.due_at is None:
            digest.undated.append(task)
        elif task.due_at < day_start:
            digest.overdue.append(task)
        elif task.due_at < day_end:
            digest.today.append(task)
        else:
            digest.upcoming.append(task)
    return digest


def daily_due(at: str | None, last_on: str | None, tz: str, now: datetime) -> bool:
    """Пора ли ежедневная рассылка: наступило время `at`, сегодня ещё не слали, не опоздали больше чем на окно."""
    if not at:
        return False
    local_now = to_local(now, tz)
    if last_on == local_now.date().isoformat():
        return False
    scheduled = datetime.combine(local_now.date(), parse_hhmm(at), tzinfo=ZoneInfo(tz))
    return scheduled <= local_now < scheduled + CATCH_UP_WINDOW


def digest_due(user: User, now: datetime) -> bool:
    return daily_due(user.digest_time, user.last_digest_on, user.timezone, now)
