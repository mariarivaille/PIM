"""Умное время: из журнала событий (task_events) выводит привычки пользователя по времени.

- в какие часы он реально делает дела и сам их назначает (отдельно будни / выходные);
- на напоминания в какие часы почти не реагирует;
- на сколько обычно откладывает.

Выводы делаются только при достаточном числе наблюдений — до этого профиль пустой и всё работает по умолчанию.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pim.core.models import TaskEvent, TaskEventKind, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import QUIET_FROM, QUIET_TO, to_local

WINDOW_DAYS = 60
MIN_HOUR_SAMPLES = 5        # наблюдений за будни/выходные, чтобы называть «обычный час»
MIN_REMINDER_SAMPLES = 3    # напоминаний в этот час, чтобы судить о реакции
BAD_RESPONSE_RATE = 0.25    # реагирует реже — час «плохой»
REACTION_WINDOW = timedelta(hours=2)
MIN_SNOOZE_SAMPLES = 3
# Варианты «отложить», к которым округляется история
SNOOZE_BUCKETS = (15, 30, 60, 120, 180, 24 * 60)


def snooze_bucket(minutes: int) -> int:
    if minutes >= 20 * 60:
        return 24 * 60
    return min(SNOOZE_BUCKETS[:-1], key=lambda b: abs(b - minutes))


@dataclass
class TimeProfile:
    weekday_hours: Counter = field(default_factory=Counter)
    weekend_hours: Counter = field(default_factory=Counter)
    # час напоминания -> (сколько отправлено, на сколько отреагировал в течение REACTION_WINDOW)
    reminders: dict[int, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    snoozes: Counter = field(default_factory=Counter)

    @property
    def bad_hours(self) -> list[int]:
        return sorted(h for h, (sent, reacted) in self.reminders.items()
                      if sent >= MIN_REMINDER_SAMPLES and reacted / sent < BAD_RESPONSE_RATE)

    def preferred_hour(self, weekend: bool) -> int | None:
        counts = self.weekend_hours if weekend else self.weekday_hours
        if sum(counts.values()) < MIN_HOUR_SAMPLES:
            return None
        bad = set(self.bad_hours)
        return next((h for h, _ in counts.most_common() if h not in bad and QUIET_TO <= h < QUIET_FROM), None)

    @property
    def usual_snooze(self) -> int | None:
        if not self.snoozes:
            return None
        minutes, count = self.snoozes.most_common(1)[0]
        return minutes if count >= MIN_SNOOZE_SAMPLES else None

    def prompt_lines(self) -> list[str]:
        lines = []
        weekday, weekend = self.preferred_hour(False), self.preferred_hour(True)
        if weekday is not None or weekend is not None:
            parts = []
            if weekday is not None:
                parts.append(f"в будни около {weekday}:00")
            if weekend is not None:
                parts.append(f"в выходные около {weekend}:00")
            lines.append("Обычно делает дела и назначает их " + ", ".join(parts) + ".")
        if bad := self.bad_hours:
            hours = ", ".join(f"{h}:00" for h in bad)
            lines.append(f"На напоминания в {hours} почти не реагирует — не ставь на это время.")
        if (snooze := self.usual_snooze) is not None:
            label = "на завтра" if snooze >= 24 * 60 else f"на {snooze // 60} ч" if snooze >= 60 else f"на {snooze} мин"
            lines.append(f"Чаще всего откладывает напоминания {label}.")
        return lines


def build_profile(events: list[TaskEvent], tz: str) -> TimeProfile:
    profile = TimeProfile()
    by_task: dict[int, list[TaskEvent]] = defaultdict(list)
    for e in events:
        if e.task_id is not None:
            by_task[e.task_id].append(e)
        local = to_local(e.at, tz)
        if e.kind in (TaskEventKind.DONE, TaskEventKind.PLANNED):
            (profile.weekend_hours if local.weekday() >= 5 else profile.weekday_hours)[local.hour] += 1
        elif e.kind == TaskEventKind.SNOOZED and e.minutes is not None:
            profile.snoozes[snooze_bucket(e.minutes)] += 1

    # реакция на напоминание: в течение окна по этой же задаче что-то сделали (выполнил, отложил, удалил)
    for task_events in by_task.values():
        for i, e in enumerate(task_events):
            if e.kind != TaskEventKind.REMINDED:
                continue
            stats = profile.reminders[to_local(e.at, tz).hour]
            stats[0] += 1
            if any(n.kind in (TaskEventKind.DONE, TaskEventKind.SNOOZED, TaskEventKind.CANCELLED)
                   and n.at - e.at <= REACTION_WINDOW for n in task_events[i + 1:]):
                stats[1] += 1
    return profile


async def load_profile(repo: Repository, user: User, now: datetime | None = None) -> TimeProfile:
    since = (now or utcnow()) - timedelta(days=WINDOW_DAYS)
    return build_profile(await repo.task_events(user.id, since), user.timezone)
