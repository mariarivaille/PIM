"""Календарь: развёртка повторяющихся событий, занятость и ближайшее свободное время. Без LLM и aiogram."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from pim.core.models import Event

# Дальше этого свободное время для дела не ищем: весь день в встречах — лучше поставить как есть
MAX_FREE_SEARCH = 12


@dataclass
class Occurrence:
    event: Event
    start: datetime  # aware UTC
    end: datetime

    @property
    def busy(self) -> bool:
        return not self.event.all_day


def occurrences(event: Event, start: datetime, end: datetime, tz: str) -> list[Occurrence]:
    """Разы события, пересекающие [start, end). Повтор считается в локальной зоне — «каждый пн в 19» не съезжает."""
    length = event.ends_at - event.starts_at
    if not event.rrule:
        if event.starts_at < end and event.ends_at > start:
            return [Occurrence(event, event.starts_at, event.ends_at)]
        return []
    zone = ZoneInfo(tz)
    local_start = event.starts_at.astimezone(zone).replace(tzinfo=None)
    rule = rrulestr(event.rrule, dtstart=local_start)
    # начало раза может быть раньше окна, а конец — внутри
    after = (start - length).astimezone(zone).replace(tzinfo=None)
    before = end.astimezone(zone).replace(tzinfo=None)
    out = []
    for local in rule.between(after, before, inc=True):
        s = local.replace(tzinfo=zone).astimezone(timezone.utc)
        if s < end and s + length > start:
            out.append(Occurrence(event, s, s + length))
    return out


def expand(events: list[Event], start: datetime, end: datetime, tz: str) -> list[Occurrence]:
    occ = [o for e in events if not e.cancelled for o in occurrences(e, start, end, tz)]
    return sorted(occ, key=lambda o: (o.start, o.event.id))


def conflicts(events: list[Event], at: datetime, tz: str, minutes: int = 0) -> list[Occurrence]:
    """Встречи, на которые попадает момент at (или отрезок at…at+minutes). События на весь день — не помеха."""
    end = at + timedelta(minutes=max(minutes, 0)) if minutes else at + timedelta(seconds=1)
    return [o for o in expand(events, at, end, tz) if o.busy]


def next_free(events: list[Event], at: datetime, tz: str) -> datetime:
    """Ближайший момент не позже цепочки встреч: at, если свободно, иначе конец встречи (и следующей, если подряд)."""
    for _ in range(MAX_FREE_SEARCH):
        busy = conflicts(events, at, tz)
        if not busy:
            return at
        at = max(o.end for o in busy)
    return at


def next_reminder(event: Event, after: datetime, tz: str, now: datetime) -> datetime | None:
    """Когда напомнить о ближайшем разе события, начинающемся позже after.

    Если момент «за N минут» уже прошёл, а сам раз ещё впереди — напомнить сразу (now)."""
    if event.remind_before is None or event.cancelled:
        return None
    lead = timedelta(minutes=event.remind_before)
    if not event.rrule:
        return max(_remind_moment(event, event.starts_at, lead, tz), now) if event.starts_at > after else None
    horizon = max(after, event.starts_at) + timedelta(days=400)
    for occ in occurrences(event, after + timedelta(seconds=1), horizon, tz):
        if occ.start <= after:
            continue
        return max(_remind_moment(event, occ.start, lead, tz), now)
    return None


ALL_DAY_REMIND_HOUR = 10


def _remind_moment(event: Event, start: datetime, lead: timedelta, tz: str) -> datetime:
    """Событие на весь день начинается в полночь — напоминаем не ночью, а в 10:00 нужного дня."""
    moment = start - lead
    if event.all_day:
        local = moment.astimezone(ZoneInfo(tz))
        moment = local.replace(hour=ALL_DAY_REMIND_HOUR, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return moment
