"""Ежедневные рассылки в заданное локальное время: утренняя сводка, вечерний разбор, важные даты."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pim.core.timeutils import to_local

# Если бот был выключен в момент рассылки, догоняем её не позже чем через это окно
CATCH_UP_WINDOW = timedelta(hours=3)


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def daily_due(at: str | None, last_on: str | None, tz: str, now: datetime) -> bool:
    """Пора ли ежедневная рассылка: наступило время `at`, сегодня ещё не слали, не опоздали больше чем на окно."""
    if not at:
        return False
    local_now = to_local(now, tz)
    if last_on == local_now.date().isoformat():
        return False
    scheduled = datetime.combine(local_now.date(), parse_hhmm(at), tzinfo=ZoneInfo(tz))
    return scheduled <= local_now < scheduled + CATCH_UP_WINDOW
