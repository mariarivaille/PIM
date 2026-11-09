import asyncio
import logging
from datetime import datetime, timedelta
from typing import Protocol

from pim.core.models import Task, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import to_local
from pim.memory.reflection import ReflectionResult, Reflector
from pim.reminders.digest import Digest, build_digest, digest_due

log = logging.getLogger(__name__)

REFLECTION_RETRY = timedelta(minutes=10)


class Notifier(Protocol):
    """Канал доставки: Telegram, позже push в приложение и т.п."""

    async def send_reminder(self, user: User, task: Task) -> None: ...

    async def send_digest(self, user: User, digest: Digest) -> None: ...

    async def send_memory_update(self, user: User, result: ReflectionResult) -> None: ...


class ReminderScheduler:
    """Детерминированный поллинг БД: всё состояние в таблицах, рестарт ничего не теряет."""

    def __init__(self, repo: Repository, notifier: Notifier, poll_seconds: int,
                 reflector: Reflector | None = None, reflection_idle_minutes: int = 10):
        self._repo = repo
        self._notifier = notifier
        self._poll = poll_seconds
        self._reflector = reflector
        self._reflection_idle = timedelta(minutes=reflection_idle_minutes)
        # если LLM недоступна, не долбим её каждый тик: следующая попытка для пользователя не раньше этого
        self._reflection_retry_at: dict[int, datetime] = {}

    async def tick(self) -> int:
        now = utcnow()
        sent = 0
        for task, user in await self._repo.due_tasks(now):
            try:
                await self._notifier.send_reminder(user, task)
            except Exception:
                # не помечаем — попробуем на следующем тике
                log.exception("failed to send reminder for task %s", task.id)
                continue
            await self._repo.mark_reminded(task.id, now)
            sent += 1
        return sent

    async def tick_digests(self) -> int:
        now = utcnow()
        sent = 0
        for user in await self._repo.users_with_digest():
            if not digest_due(user, now):
                continue
            try:
                await self._notifier.send_digest(user, await build_digest(self._repo, user, now))
            except Exception:
                log.exception("failed to send digest to user %s", user.id)
                continue
            await self._repo.mark_digest_sent(user.id, to_local(now, user.timezone).date().isoformat())
            sent += 1
        return sent

    async def tick_reflection(self) -> int:
        """Разбирает затихшие разговоры и сообщает пользователю, что запомнил."""
        if self._reflector is None:
            return 0
        now = utcnow()
        changed = 0
        for user in await self._repo.users_pending_reflection(idle_before=now - self._reflection_idle):
            if self._reflection_retry_at.get(user.id, now) > now:
                continue
            try:
                result = await self._reflector.reflect(user)
            except Exception:
                log.exception("reflection failed for user %s", user.id)
                self._reflection_retry_at[user.id] = now + REFLECTION_RETRY
                continue
            self._reflection_retry_at.pop(user.id, None)
            if result.empty:
                continue
            try:
                await self._notifier.send_memory_update(user, result)
            except Exception:
                log.exception("failed to send memory update to user %s", user.id)
            changed += 1
        return changed

    async def run_forever(self) -> None:
        log.info("reminder scheduler started, poll every %ss", self._poll)
        while True:
            for job in (self.tick, self.tick_digests, self.tick_reflection):
                try:
                    await job()
                except Exception:
                    log.exception("scheduler job %s failed", job.__name__)
            await asyncio.sleep(self._poll)
