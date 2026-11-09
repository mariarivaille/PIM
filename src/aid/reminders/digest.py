"""Утренняя сводка: собирается из БД детерминированно, без LLM."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from aid.core.models import Task, User
from aid.core.repository import Repository
from aid.core.timeutils import to_local

# Если бот был выключен в момент сводки, догоняем её не позже чем через это окно
CATCH_UP_WINDOW = timedelta(hours=3)


@dataclass
class Digest:
    day: datetime  # полночь текущего локального дня
    overdue: list[Task] = field(default_factory=list)
    today: list[Task] = field(default_factory=list)
    undated: list[Task] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.overdue or self.today or self.undated)


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
    return digest


def digest_due(user: User, now: datetime) -> bool:
    """Пора ли слать сводку: наступило время, ещё не слали сегодня, не опоздали больше чем на окно."""
    if not user.digest_time:
        return False
    local_now = to_local(now, user.timezone)
    if user.last_digest_on == local_now.date().isoformat():
        return False
    scheduled = datetime.combine(local_now.date(), parse_hhmm(user.digest_time), tzinfo=ZoneInfo(user.timezone))
    return scheduled <= local_now < scheduled + CATCH_UP_WINDOW
