from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def parse_local(value: str, tz: str) -> datetime:
    """ISO-строка от LLM -> aware UTC. Без смещения трактуется как локальное время пользователя."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def format_local(dt: datetime, tz: str) -> str:
    local = to_local(dt, tz)
    return f"{WEEKDAYS_RU[local.weekday()]} {local:%d.%m %H:%M}"


DAY_KEYWORDS = ["today", "tomorrow", "day_after_tomorrow", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_TIME = time(10, 0)


def resolve_day_time(now: datetime, tz: str, day: str | None, at: time | None) -> datetime:
    """День словом + время -> aware UTC. Дни недели — ближайший будущий (сегодняшний день недели = через неделю)."""
    local_now = to_local(now, tz)
    today = local_now.date()
    if day is None or day == "today":
        target = today
    elif day == "tomorrow":
        target = today + timedelta(days=1)
    elif day == "day_after_tomorrow":
        target = today + timedelta(days=2)
    else:
        weekday = DAY_KEYWORDS.index(day) - 3
        target = today + timedelta(days=(weekday - today.weekday() - 1) % 7 + 1)
    result = datetime.combine(target, at or DEFAULT_TIME, tzinfo=ZoneInfo(tz))
    # «в 9» поздним вечером без указания дня — это завтра
    if day is None and result <= local_now:
        result += timedelta(days=1)
    return result.astimezone(timezone.utc)


def next_occurrence(rule: str, prev: datetime, tz: str, after: datetime) -> datetime | None:
    """Следующее срабатывание повторяющейся задачи строго после `after`.

    Правило считается в локальной таймзоне, чтобы «каждый день в 9:00» не съезжало при смене DST.
    """
    local_start = to_local(prev, tz).replace(tzinfo=None)
    local_after = to_local(after, tz).replace(tzinfo=None)
    nxt = rrulestr(rule, dtstart=local_start).after(local_after)
    if nxt is None:
        return None
    return nxt.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
