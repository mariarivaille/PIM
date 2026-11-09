"""Утренняя сводка: собирается из БД детерминированно, без LLM."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pim.core.models import Task, User
from pim.core.repository import Repository
from pim.core.timeutils import to_local
from pim.reminders.daily import daily_due
from pim.reminders.dates import UpcomingDate, user_upcoming_dates

DATES_HORIZON = 14

@dataclass
class Digest:
    day: datetime  # полночь текущего локального дня
    overdue: list[Task] = field(default_factory=list)
    today: list[Task] = field(default_factory=list)
    upcoming: list[Task] = field(default_factory=list)
    undated: list[Task] = field(default_factory=list)
    # важные даты из памяти (ДР, годовщины) на ближайшие DATES_HORIZON дней
    dates: list[UpcomingDate] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.overdue or self.today or self.upcoming or self.undated or self.dates)


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
    digest.dates = await user_upcoming_dates(repo, user, now, DATES_HORIZON)
    return digest


def digest_due(user: User, now: datetime) -> bool:
    return daily_due(user.digest_time, user.last_digest_on, user.timezone, now)
