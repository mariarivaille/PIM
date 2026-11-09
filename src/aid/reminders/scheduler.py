import asyncio
import logging
from typing import Protocol

from aid.core.models import Task, User, utcnow
from aid.core.repository import Repository

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Канал доставки напоминаний: Telegram, позже push в приложение и т.п."""

    async def send_reminder(self, user: User, task: Task) -> None: ...


class ReminderScheduler:
    """Детерминированный поллинг БД: всё состояние в таблице tasks, рестарт ничего не теряет."""

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

    async def run_forever(self) -> None:
        log.info("reminder scheduler started, poll every %ss", self._poll)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._poll)
