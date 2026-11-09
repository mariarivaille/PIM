"""Проактивные даты: бот сам вспоминает дни рождения и годовщины из памяти и предупреждает заранее."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pim.core.models import Memory, User
from pim.core.repository import Repository
from pim.core.timeutils import nearest_reasonable_time, plural_ru, to_local
from pim.core.yearly import next_occurrence
from pim.reminders.daily import daily_due

# За сколько дней предупреждать: за неделю (успеть с подарком), накануне и в сам день
LEADS = (7, 1, 0)
# Когда проверять, если утренняя сводка выключена
DEFAULT_CHECK_TIME = "10:00"


@dataclass
class UpcomingDate:
    memory: Memory
    occurs: date
    days: int  # через сколько дней (0 — сегодня)
    age: int | None = None  # сколько исполнится / лет событию, если известен год


def upcoming_dates(memories: list[Memory], today: date, horizon_days: int) -> list[UpcomingDate]:
    result = []
    for m in memories:
        if m.date_month is None or m.date_day is None:
            continue
        occurs = next_occurrence(m.date_month, m.date_day, today)
        days = (occurs - today).days
        if days <= horizon_days:
            age = occurs.year - m.date_year if m.date_year and m.date_year < occurs.year else None
            result.append(UpcomingDate(memory=m, occurs=occurs, days=days, age=age))
    return sorted(result, key=lambda u: (u.days, u.memory.number or 0))


async def user_upcoming_dates(repo: Repository, user: User, now: datetime, horizon_days: int) -> list[UpcomingDate]:
    today = to_local(now, user.timezone).date()
    return upcoming_dates(await repo.memories_with_dates(user.id), today, horizon_days)


def dates_due(user: User, now: datetime) -> bool:
    """Проверяем раз в день — в момент утренней сводки (или в 10:00, если сводка выключена)."""
    return daily_due(user.digest_time or DEFAULT_CHECK_TIME, user.last_dates_on, user.timezone, now)


def alerts_for_today(upcoming: list[UpcomingDate]) -> list[UpcomingDate]:
    return [u for u in upcoming if u.days in LEADS]


def occurrence_key(occurs: date) -> str:
    return occurs.isoformat()


def days_until_text(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    return f"через {days} {plural_ru(days, 'день', 'дня', 'дней')}"


def reminder_at(occurs: date, offset: timedelta, now: datetime, tz: str) -> datetime:
    """Момент напоминания для кнопки; если он уже прошёл — ближайшее разумное время."""
    due = (datetime.combine(occurs, time(0), tzinfo=ZoneInfo(tz)) + offset).astimezone(timezone.utc)
    return due if due > now else nearest_reasonable_time(now, tz)


# Когда ставить дела по кнопкам — смещение от полуночи дня события:
# про подарок — за 2 дня вечером, поздравить — в сам день утром
GIFT_OFFSET = timedelta(days=-2, hours=19)
CONGRATS_OFFSET = timedelta(hours=10)
