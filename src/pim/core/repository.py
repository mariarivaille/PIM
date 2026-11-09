from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pim.core.models import ChatMessage, Memory, MemoryKind, MemorySource, Task, TaskStatus, User, utcnow
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

    # --- memories ---

    async def add_memory(self, user_id: int, content: str, kind: MemoryKind = MemoryKind.FACT,
                         source: MemorySource = MemorySource.EXPLICIT,
                         expires_at: datetime | None = None) -> Memory:
        async with self._sm() as s, s.begin():
            memory = Memory(user_id=user_id, content=content, kind=kind, source=source, expires_at=expires_at)
            s.add(memory)
        return memory

    async def get_memory(self, user_id: int, memory_id: int) -> Memory | None:
        async with self._sm() as s:
            memory = await s.get(Memory, memory_id)
            return memory if memory and memory.user_id == user_id else None

    async def list_memories(self, user_id: int, now: datetime | None = None) -> list[Memory]:
        """Актуальные воспоминания пользователя (истёкшие не возвращаются)."""
        now = now or utcnow()
        async with self._sm() as s:
            res = await s.scalars(
                select(Memory)
                .where(Memory.user_id == user_id, or_(Memory.expires_at.is_(None), Memory.expires_at > now))
                .order_by(Memory.kind, Memory.id)
            )
            return list(res)

    async def update_memory(self, user_id: int, memory_id: int, **fields) -> Memory | None:
        async with self._sm() as s, s.begin():
            memory = await s.get(Memory, memory_id)
            if not memory or memory.user_id != user_id:
                return None
            for k, v in fields.items():
                setattr(memory, k, v)
            return memory

    async def delete_memory(self, user_id: int, memory_id: int) -> Memory | None:
        async with self._sm() as s, s.begin():
            memory = await s.get(Memory, memory_id)
            if not memory or memory.user_id != user_id:
                return None
            await s.delete(memory)
            return memory

    # --- chat history ---

    async def add_messages(self, user_id: int, *items: tuple[str, str], at: datetime | None = None) -> None:
        async with self._sm() as s, s.begin():
            at = at or utcnow()
            s.add_all(ChatMessage(user_id=user_id, role=role, content=content, created_at=at)
                      for role, content in items)

    async def recent_messages(self, user_id: int, limit: int) -> list[ChatMessage]:
        """Последние реплики после /reset, в хронологическом порядке."""
        async with self._sm() as s:
            user = await s.get(User, user_id)
            query = select(ChatMessage).where(ChatMessage.user_id == user_id)
            if user and user.context_since:
                query = query.where(ChatMessage.created_at > user.context_since)
            res = await s.scalars(query.order_by(ChatMessage.id.desc()).limit(limit))
            return list(reversed(list(res)))

    async def reset_context(self, user_id: int, at: datetime | None = None) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.context_since = at or utcnow()

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
