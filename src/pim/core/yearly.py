"""Ежегодные даты из текста памяти: «Мама — Ольга, ДР 14 марта», «годовщина свадьбы 02.08.2015».

Разбирает код, а не модель: детерминированно, бесплатно и сразу для уже сохранённых фактов.
Годовой считается только дата рядом со словом-признаком (ДР, годовщина, юбилей…) — иначе
«в отпуске до 12 ноября» напоминало бы каждый год.
"""

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_RE = r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"
_WORD_DATE = re.compile(rf"\b(\d{{1,2}})\s+{_MONTH_RE}(?:\s+(\d{{4}}))?", re.IGNORECASE)
_NUM_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?\b")
YEARLY_MARKERS = re.compile(
    r"\bдр\b|д\.\s?р\.?|день рожд|дня рожд|днюх|родил|годовщин|юбиле|свадьб|именин|день памяти",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class YearlyDate:
    month: int
    day: int
    year: int | None = None


def _month_number(word: str) -> int:
    word = word.casefold()
    return next(n for prefix, n in MONTHS.items() if word.startswith(prefix))


def _valid(month: int, day: int) -> bool:
    if not 1 <= month <= 12:
        return False
    try:
        date(2000, month, day)  # високосный год — чтобы 29 февраля было допустимо
    except ValueError:
        return False
    return True


def parse_yearly_date(text: str) -> YearlyDate | None:
    if not YEARLY_MARKERS.search(text):
        return None
    if m := _WORD_DATE.search(text):
        day, month, year = int(m.group(1)), _month_number(m.group(2)), m.group(3)
    elif m := _NUM_DATE.search(text):
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if year and len(year) == 2:
            year = None  # «02.08.15» — неоднозначно, год не берём
    else:
        return None
    if not _valid(month, day):
        return None
    return YearlyDate(month=month, day=day, year=int(year) if year else None)


def occurrence_in(year: int, month: int, day: int) -> date:
    """Дата в конкретном году; 29 февраля в невисокосный — 28-е."""
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, day - 1)


def next_occurrence(month: int, day: int, today: date) -> date:
    """Ближайшее наступление даты начиная с сегодняшнего дня (сегодня — тоже считается)."""
    this_year = occurrence_in(today.year, month, day)
    return this_year if this_year >= today else occurrence_in(today.year + 1, month, day)
