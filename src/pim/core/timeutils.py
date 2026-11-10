import math
import re
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


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def humanize_until(due: datetime, now: datetime, tz: str) -> str:
    """«через 25 мин», «через 2 ч 15 мин», «через 3 дня»; пусто, если момент уже прошёл."""
    seconds = (due - now).total_seconds()
    if seconds <= 0:
        return ""
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"через {minutes} мин"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"через {hours} ч" + (f" {rest} мин" if rest else "")
    days = (to_local(due, tz).date() - to_local(now, tz).date()).days
    return f"через {days} {plural_ru(days, 'день', 'дня', 'дней')}"


# Запасное время, если модель не выбрала его сама: не ночью и не в прошлом
QUIET_FROM, QUIET_TO = 22, 8


def nearest_reasonable_time(now: datetime, tz: str) -> datetime:
    """Через час (с округлением до :00/:30), а если это попадает на ночь — завтра в DEFAULT_TIME."""
    local = to_local(now, tz) + timedelta(hours=1)
    bump = (30 - local.minute % 30) % 30
    local = (local + timedelta(minutes=bump)).replace(second=0, microsecond=0)
    if local.hour >= QUIET_FROM or local.hour < QUIET_TO:
        day = local.date() if local.hour < QUIET_TO else local.date() + timedelta(days=1)
        local = datetime.combine(day, DEFAULT_TIME, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)


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


# Указания времени в названии дела: время живёт в due_at, а в названии оно устаревает при переносе
# («обнять в обед», а дело уже на 20:00)
_WEEKDAYS_ACC = r"понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье"
_TIME_PHRASES = re.compile(
    r"(?<!\w)(?:"
    r"сегодня|завтра|послезавтра|"
    r"с утра|утром|днём|днем|вечером|ночью|"
    r"в обед|на обеде|после обеда|перед обедом|в обеденный перерыв|"
    r"на выходных|в выходные|"
    rf"(?:в|во|до|к|после) (?:{_WEEKDAYS_ACC})|"
    # «в 19:00», «в 7 утра», «к 10» в конце фразы — время; «в 2 этаже» — нет
    r"(?:в|к|до|после) \d{1,2}(?:[:.]\d{2}(?: (?:часов|часа|час|утра|вечера|дня|ночи))?"
    r"| (?:часов|часа|час|утра|вечера|дня|ночи)|(?=\s*(?:$|[,.!?;])))|"
    r"через (?:полчаса|час|пару часов|\d+ (?:минут[уы]?|час(?:а|ов)?|дн(?:я|ей)|день))"
    r")(?!\w)",
    re.IGNORECASE,
)


def strip_time_words(title: str) -> str:
    """Убирает из названия дела слова о времени и дне; если ничего не осталось — возвращает как было."""
    cleaned = _TIME_PHRASES.sub(" ", title)
    cleaned = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", cleaned)).strip(" ,.-—")
    return cleaned if len(cleaned) >= 2 else title
