from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pim.core.models import Task, TaskStatus, User, utcnow
from pim.core.timeutils import next_occurrence


class Repository:
    """Единая точка доступа к данным. Каждый метод — своя короткая транзакция."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self._sm = sessionmaker

    # --- users ---

    async def get_or_create_user(self, user_id: int, chat_id: int, default_tz: str) -> User:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user is None:
                user = User(id=user_id, chat_id=chat_id, timezone=default_tz)
                s.add(user)
            elif user.chat_id != chat_id:
                user.chat_id = chat_id
            return user

    async def get_user(self, user_id: int) -> User | None:
        async with self._sm() as s:
            return await s.get(User, user_id)

    async def set_timezone(self, user_id: int, tz: str) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.timezone = tz

    async def set_digest_time(self, user_id: int, value: str | None) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.digest_time = value

    async def users_with_digest(self) -> list[User]:
        async with self._sm() as s:
            return list(await s.scalars(select(User).where(User.digest_time.is_not(None))))

    async def mark_digest_sent(self, user_id: int, local_date: str) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.last_digest_on = local_date

    # --- tasks ---

    async def create_task(
        self,
        user_id: int,
        title: str,
        due_at: datetime | None = None,
        rrule: str | None = None,
        notes: str | None = None,
    ) -> Task:
        async with self._sm() as s, s.begin():
            task = Task(user_id=user_id, title=title, due_at=due_at, rrule=rrule, notes=notes)
            s.add(task)
        return task

    async def get_task(self, user_id: int, task_id: int) -> Task | None:
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            return task if task and task.user_id == user_id else None

    async def list_open_tasks(self, user_id: int) -> list[Task]:
        async with self._sm() as s:
            res = await s.scalars(
                select(Task)
                .where(Task.user_id == user_id, Task.status == TaskStatus.OPEN)
                .order_by(Task.due_at.is_(None), Task.due_at, Task.id)
            )
            return list(res)

    async def update_task(self, user_id: int, task_id: int, **fields) -> Task | None:
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if not task or task.user_id != user_id:
                return None
            for k, v in fields.items():
                setattr(task, k, v)
            if "due_at" in fields:
                task.reminded_at = None
            return task

    async def complete_task(self, user_id: int, task_id: int, tz: str) -> Task | None:
        """Закрывает задачу. Повторяющуюся — переносит на следующее срабатывание."""
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if not task or task.user_id != user_id or task.status != TaskStatus.OPEN:
                return None
            now = utcnow()
            nxt = None
            if task.rrule and task.due_at:
                nxt = next_occurrence(task.rrule, task.due_at, tz, after=max(now, task.due_at))
            if nxt:
                task.due_at = nxt
                task.reminded_at = None
            else:
                task.status = TaskStatus.DONE
                task.completed_at = now
            return task

    async def cancel_task(self, user_id: int, task_id: int) -> Task | None:
        return await self.update_task(user_id, task_id, status=TaskStatus.CANCELLED)

    async def snooze_task(self, user_id: int, task_id: int, until: datetime) -> Task | None:
        return await self.update_task(user_id, task_id, due_at=until)

    async def due_tasks(self, now: datetime) -> list[tuple[Task, User]]:
        """Открытые задачи, по которым пора напомнить и ещё не напомнили."""
        async with self._sm() as s:
            res = await s.execute(
                select(Task, User)
                .join(User, User.id == Task.user_id)
                .where(
                    Task.status == TaskStatus.OPEN,
                    Task.due_at.is_not(None),
                    Task.due_at <= now,
                    Task.reminded_at.is_(None),
                )
                .order_by(Task.due_at)
            )
            return [(t, u) for t, u in res.all()]

    async def mark_reminded(self, task_id: int, at: datetime) -> None:
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if task:
                task.reminded_at = at
