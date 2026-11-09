import asyncio
import logging
from datetime import datetime, timedelta
from typing import Protocol

from pim.core.models import ItemList, ListItem, Task, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import to_local
from pim.learning.timing import TimeProfile, load_profile
from pim.memory.reflection import Reflector
from pim.reminders.digest import Digest, build_digest, digest_due
from pim.reminders.dates import UpcomingDate, alerts_for_today, dates_due, occurrence_key, user_upcoming_dates
from pim.reminders.review import Review, build_review, review_due

log = logging.getLogger(__name__)

REFLECTION_RETRY = timedelta(minutes=10)


class Notifier(Protocol):
    """Канал доставки: Telegram, позже push в приложение и т.п."""

    async def send_reminder(self, user: User, task: Task, usual_snooze: int | None = None) -> None: ...

    async def send_digest(self, user: User, digest: Digest) -> None: ...

    async def send_review(self, user: User, review: Review) -> None: ...

    async def send_date_alert(self, user: User, upcoming: UpcomingDate) -> None: ...

    async def send_assignment(self, assignee: User, task: Task, by: User) -> None: ...

    async def send_outcome(self, by: User, task: Task, assignee: User) -> None: ...

    async def send_list_additions(self, recipient: User, lst: ItemList, adder: User, items: list[ListItem]) -> None: ...


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
        profiles: dict[int, TimeProfile] = {}
        for task, user in await self._repo.due_tasks(now):
            if user.id not in profiles:
                profiles[user.id] = await load_profile(self._repo, user, now)
            try:
                await self._notifier.send_reminder(user, task, usual_snooze=profiles[user.id].usual_snooze)
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

    async def tick_reviews(self) -> int:
        """Вечерний разбор: несделанное за сегодня — с кнопками «сделано / на завтра / удалить»."""
        now = utcnow()
        sent = 0
        for user in await self._repo.users_with_review():
            if not review_due(user, now):
                continue
            review = await build_review(self._repo, user, now)
            if review.should_send:
                try:
                    await self._notifier.send_review(user, review)
                except Exception:
                    log.exception("failed to send review to user %s", user.id)
                    continue
                sent += 1
            # и когда разбирать нечего — помечаем день, чтобы не проверять каждые 30 секунд
            await self._repo.mark_review_sent(user.id, to_local(now, user.timezone).date().isoformat())
        return sent

    async def tick_dates(self) -> int:
        """Раз в день: дни рождения и годовщины из памяти — за неделю, накануне и в сам день."""
        now = utcnow()
        sent = 0
        for user in await self._repo.users_for_dates():
            if not dates_due(user, now):
                continue
            for up in alerts_for_today(await user_upcoming_dates(self._repo, user, now, horizon_days=7)):
                key = occurrence_key(up.occurs)
                if await self._repo.date_alert_sent(up.memory.id, key, up.days):
                    continue
                try:
                    await self._notifier.send_date_alert(user, up)
                except Exception:
                    log.exception("failed to send date alert to user %s", user.id)
                    continue
                await self._repo.record_date_alert(up.memory.id, key, up.days)
                sent += 1
            await self._repo.mark_dates_checked(user.id, to_local(now, user.timezone).date().isoformat())
        return sent

    async def tick_shared(self) -> int:
        """Поручения близким и общие списки: кому что сообщить. Флаги в БД — ничего не теряется при рестарте."""
        sent = 0
        for task, assignee, by in await self._repo.pending_assignments():
            try:
                await self._notifier.send_assignment(assignee, task, by)
            except Exception:
                log.exception("failed to send assignment %s", task.id)
                continue
            await self._repo.mark_task_notified(task.id, "assign_notified_at")
            sent += 1
        for task, assignee, by in await self._repo.pending_outcomes():
            try:
                await self._notifier.send_outcome(by, task, assignee)
            except Exception:
                log.exception("failed to send outcome of %s", task.id)
                continue
            await self._repo.mark_task_notified(task.id, "outcome_notified_at")
            sent += 1
        for lst, adder_id, items in await self._repo.pending_list_additions():
            adder = await self._repo.get_user(adder_id)
            for recipient_id in await self._repo.list_participants(lst.id):
                if recipient_id == adder_id:
                    continue
                recipient = await self._repo.get_user(recipient_id)
                try:
                    await self._notifier.send_list_additions(recipient, lst, adder, items)
                    sent += 1
                except Exception:
                    log.exception("failed to send list additions to %s", recipient_id)
            await self._repo.mark_items_notified([i.id for i in items])
        return sent

    async def tick_reflection(self) -> int:
        """Разбирает затихшие разговоры и молча обновляет память (пользователь видит её только в /memory)."""
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
            if not result.empty:
                changed += 1
        return changed

    async def run_forever(self) -> None:
        log.info("reminder scheduler started, poll every %ss", self._poll)
        while True:
            for job in (self.tick, self.tick_shared, self.tick_digests, self.tick_dates, self.tick_reviews,
                        self.tick_reflection):
                try:
                    await job()
                except Exception:
                    log.exception("scheduler job %s failed", job.__name__)
            await asyncio.sleep(self._poll)
