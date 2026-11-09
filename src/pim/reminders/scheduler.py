import asyncio
import logging
from typing import Protocol

from pim.core.models import Task, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import to_local
from pim.reminders.digest import Digest, build_digest, digest_due

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Канал доставки: Telegram, позже push в приложение и т.п."""

    async def send_reminder(self, user: User, task: Task) -> None: ...

    async def send_digest(self, user: User, digest: Digest) -> None: ...


class ReminderScheduler:
    """Детерминированный поллинг БД: всё состояние в таблицах, рестарт ничего не теряет."""

    def __init__(self, repo: Repository, notifier: Notifier, poll_seconds: int):
        self._repo = repo
        self._notifier = notifier
        self._poll = poll_seconds

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

    async def run_forever(self) -> None:
        log.info("reminder scheduler started, poll every %ss", self._poll)
        while True:
            for job in (self.tick, self.tick_digests):
                try:
                    await job()
                except Exception:
                    log.exception("scheduler job %s failed", job.__name__)
            await asyncio.sleep(self._poll)
