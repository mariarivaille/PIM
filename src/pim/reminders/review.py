"""Вечерний разбор «что переносим?»: несделанное за сегодня и просроченное — с кнопками. Без LLM."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pim.core.models import Task, TaskStatus, User
from pim.core.repository import Repository
from pim.core.timeutils import to_local
from pim.reminders.digest import daily_due


@dataclass
class Review:
    pending: list[Task] = field(default_factory=list)  # открытые дела со сроком до конца сегодня
    done_today: int = 0

    @property
    def should_send(self) -> bool:
        """Есть что разбирать — или всё закрыто и можно порадоваться. Если дел не было вовсе — молчим."""
        return bool(self.pending or self.done_today)


def _day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    start = to_local(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def build_review(repo: Repository, user: User, now: datetime) -> Review:
    day_start, day_end = _day_bounds(now, user.timezone)
    pending = [t for t in await repo.list_open_tasks(user.id) if t.due_at is not None and t.due_at < day_end]
    done_today = await repo.count_completed(user.id, day_start, day_end)
    return Review(pending=pending, done_today=done_today)


def review_due(user: User, now: datetime) -> bool:
    return daily_due(user.review_time, user.last_review_on, user.timezone, now)


def tomorrow_same_time(due: datetime, now: datetime, tz: str) -> datetime:
    """Тот же час и минута, но завтрашнее (относительно «сейчас») число — для «➡️ На завтра»."""
    local_due = to_local(due, tz)
    tomorrow = to_local(now, tz).date() + timedelta(days=1)
    moved = datetime.combine(tomorrow, local_due.time().replace(tzinfo=None), tzinfo=ZoneInfo(tz))
    return moved.astimezone(ZoneInfo("UTC"))


async def postpone_to_tomorrow(repo: Repository, user: User, task_id: int, now: datetime) -> Task | None:
    task = await repo.get_task(user.id, task_id)
    if task is None or task.status != TaskStatus.OPEN or task.due_at is None:
        return None
    return await repo.snooze_task(user.id, task_id, tomorrow_same_time(task.due_at, now, user.timezone))


async def postpone_all_to_tomorrow(repo: Repository, user: User, now: datetime) -> list[Task]:
    """Переносит на завтра всё, что сейчас висит в разборе (список берётся заново на момент нажатия)."""
    moved = []
    for task in (await build_review(repo, user, now)).pending:
        if updated := await postpone_to_tomorrow(repo, user, task.id, now):
            moved.append(updated)
    return moved
