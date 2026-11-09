from datetime import datetime, timezone
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
