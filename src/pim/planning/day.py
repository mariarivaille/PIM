"""«Спланируй мой день»: модель раскладывает дела без времени и просроченные по свободным окнам дня,
код проверяет план (не в прошлом, не ночью, без пересечений) и сохраняет его до подтверждения пользователем.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pim.core.models import Task, TaskStatus, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import QUIET_FROM, QUIET_TO, WEEKDAYS_RU, to_local
from pim.learning.timing import load_profile
from pim.llm.base import LLMProvider, Message
from pim.llm.jsonout import extract_json

log = logging.getLogger(__name__)

MAX_FLEXIBLE = 12
FIXED_BLOCK = timedelta(minutes=30)  # сколько считаем занятым вокруг дела с фиксированным временем
STEP = timedelta(minutes=15)
DEFAULT_MINUTES = 30
# Дальше этого из-за пересечений не сдвигаем: «записаться к врачу» в 19:00 → 21:00 уже бессмысленно —
# лучше в «не влезло»
MAX_SHIFT = timedelta(hours=1)
MAX_MINUTES = 240

PLAN_PROMPT = """\
Ты планируешь день пользователя личного ассистента. День: {day_title}. {now_line}

Что известно о пользователе:
{memories}

Относится именно к этому дню недели (обязательно учти в busy):
{day_facts}

Привычки по времени:
{habits}

Уже стоит на этот день (время не трогай, не ставь на него ничего):
{fixed}

Дела, которые нужно разместить (id: название — заметки):
{flexible}

Сначала из того, что известно о пользователе, определи для ЭТОГО дня недели:
- wake — во сколько он встаёт (если неизвестно — пустая строка);
- busy — когда он точно занят: работа, спорт, дорога, регулярные дела именно в этот день недели.
  Если время регулярного дела не уточнено («вечером», «утром») — займи под него разумный интервал.

Потом составь реалистичный план: разложи дела по свободному времени с учётом смысла дел и привычек.
Звонки и запись в организации (банк, врач, госуслуги) — только с 9:00 до 18:00, в обед или до/после работы;
покупки и быт — вечером после работы; важное и тяжёлое — когда больше сил. Оцени длительность каждого дела
в минутах. Ничего не ставь раньше {earliest}, раньше wake, позже {latest} и на время busy.
Если всё реалистично не влезает — лишнее перечисли в skip, не впихивай.

Ответь ТОЛЬКО JSON, без пояснений и markdown:
{{"wake": "10:00", "busy": [{{"from": "11:00", "to": "19:00", "what": "работа"}}],
 "plan": [{{"task_id": 3, "time": "HH:MM", "minutes": 30}}], "skip": [5],
 "comment": "одна короткая дружелюбная фраза о плане"}}
"""


# Как в тексте памяти упоминаются дни недели (индекс = weekday()): «по вт и чт», «в четверг», «по будням»
_WEEKDAY_WORDS = [r"пн|понедельник", r"вт|вторник", r"ср|сред", r"чт|четверг", r"пт|пятниц", r"сб|суббот",
                  r"вс|воскресен"]


def facts_for_weekday(facts: list[str], weekday: int) -> list[str]:
    """Факты памяти, которые явно касаются этого дня недели — модели их надо подсветить, сама она их упускает."""
    patterns = [_WEEKDAY_WORDS[weekday], r"будн|рабоч\w* д" if weekday < 5 else r"выходн"]
    rx = re.compile(r"(?<!\w)(" + "|".join(patterns) + r")", re.IGNORECASE)
    return [f for f in facts if rx.search(f)]


@dataclass
class PlannedItem:
    task: Task
    at: datetime  # UTC
    minutes: int


@dataclass
class Plan:
    day: date
    fixed: list[Task] = field(default_factory=list)
    items: list[PlannedItem] = field(default_factory=list)
    skipped: list[Task] = field(default_factory=list)
    comment: str = ""
    id: int | None = None

    @property
    def empty(self) -> bool:
        return not self.items


def _round_up(dt: datetime) -> datetime:
    extra = (-dt.minute) % 15
    return (dt + timedelta(minutes=extra)).replace(second=0, microsecond=0)


def _day_bounds(day: date, tz: str, now: datetime) -> tuple[datetime, datetime]:
    """Окно, в которое можно ставить дела: с 8:00 (или «через 15 минут», если это сегодня) до 22:00."""
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time(QUIET_TO), tzinfo=zone)
    end = datetime.combine(day, time(QUIET_FROM), tzinfo=zone)
    start = max(start, _round_up(to_local(now, tz) + timedelta(minutes=15)))
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def gather(repo: Repository, user: User, day: date) -> tuple[list[Task], list[Task]]:
    """(фиксированные дела этого дня, дела для планирования: без срока и просроченные)."""
    zone = ZoneInfo(user.timezone)
    day_start = datetime.combine(day, time(0), tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    fixed, overdue, undated = [], [], []
    for t in await repo.list_open_tasks(user.id):
        if t.due_at is None:
            undated.append(t)
        elif t.due_at < day_start:
            overdue.append(t)
        elif t.due_at < day_end:
            fixed.append(t)
    return fixed, (overdue + undated)[:MAX_FLEXIBLE]


def _local_time(day: date, value, zone: ZoneInfo) -> datetime | None:
    """«HH:MM» этого дня -> UTC; None, если не разобрать."""
    try:
        hh, mm = str(value).strip().split(":")
        return datetime.combine(day, time(int(hh), int(mm)), tzinfo=zone).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def constraints(proposal: dict, day: date, tz: str) -> tuple[datetime | None, list[tuple[datetime, datetime]]]:
    """Подъём и занятые интервалы дня, которые модель вывела из памяти: их соблюдает уже код, а не модель."""
    zone = ZoneInfo(tz)
    wake = _local_time(day, proposal.get("wake"), zone) if proposal.get("wake") else None
    busy = []
    for block in proposal.get("busy") or []:
        if not isinstance(block, dict):
            continue
        start, end = _local_time(day, block.get("from"), zone), _local_time(day, block.get("to"), zone)
        if start and end and start < end:
            busy.append((start, end))
    return wake, busy


def schedule(proposal: dict, flexible: list[Task], fixed: list[Task], day: date, tz: str,
             now: datetime) -> tuple[list[PlannedItem], list[Task]]:
    """Проверяет предложение модели: только известные дела, в окне дня (не раньше подъёма), без пересечений
    с фиксированными делами, занятостью и друг с другом. Остальное — в «не влезло»."""
    by_id = {t.id: t for t in flexible}
    window_start, window_end = _day_bounds(day, tz, now)
    wake, declared_busy = constraints(proposal, day, tz)
    if wake:
        window_start = max(window_start, wake)
    busy = [(t.due_at, t.due_at + FIXED_BLOCK) for t in fixed] + declared_busy
    zone = ZoneInfo(tz)

    wanted = []
    for raw in proposal.get("plan") or []:
        if not isinstance(raw, dict) or raw.get("task_id") not in by_id:
            continue
        at = _local_time(day, raw.get("time", ""), zone)
        if at is None:
            continue
        minutes = raw.get("minutes")
        minutes = min(max(int(minutes), 5), MAX_MINUTES) if isinstance(minutes, (int, float)) else DEFAULT_MINUTES
        wanted.append((at, by_id.pop(raw["task_id"]), minutes))

    items, skipped = [], []
    for at, task, minutes in sorted(wanted, key=lambda w: w[0]):
        start = base = max(at, window_start)  # подтянуть к началу окна (сейчас / подъём) — нормально
        moved = True
        while moved:  # сдвигаем вперёд, пока не найдём свободное место
            moved = False
            for b_start, b_end in busy:
                if start < b_end and start + timedelta(minutes=minutes) > b_start:
                    start = _round_up(b_end)
                    moved = True
        if start + timedelta(minutes=minutes) > window_end or start - base > MAX_SHIFT:
            skipped.append(task)
            continue
        busy.append((start, start + timedelta(minutes=minutes)))
        items.append(PlannedItem(task=task, at=start, minutes=minutes))
    # то, что модель не разместила (или сама отправила в skip) — тоже «не влезло»
    skipped += list(by_id.values())
    return items, skipped


class DayPlanner:
    def __init__(self, llm: LLMProvider, repo: Repository):
        self._llm = llm
        self._repo = repo

    async def plan(self, user: User, day: date, now: datetime | None = None) -> Plan:
        now = now or utcnow()
        fixed, flexible = await gather(self._repo, user, day)
        plan = Plan(day=day, fixed=fixed)
        if not flexible:
            return plan

        tz = user.timezone
        window_start, window_end = _day_bounds(day, tz, now)
        if window_start >= window_end:
            plan.skipped = flexible
            plan.comment = "На этот день времени уже не осталось."
            return plan

        memories = await self._repo.list_memories(user.id)
        habits = (await load_profile(self._repo, user, now)).prompt_lines()
        local_now = to_local(now, tz)
        prompt = PLAN_PROMPT.format(
            day_title=f"{WEEKDAYS_RU[day.weekday()]} {day:%d.%m.%Y}",
            now_line=f"Сейчас {local_now:%H:%M}." if local_now.date() == day else "",
            memories="\n".join(f"- {m.content}" for m in memories) or "(ничего)",
            day_facts="\n".join(f"- {f}" for f in facts_for_weekday([m.content for m in memories], day.weekday()))
            or "(ничего особенного)",
            habits="\n".join(f"- {h}" for h in habits) or "(пока мало данных)",
            fixed="\n".join(f"- {to_local(t.due_at, tz):%H:%M} {t.title}" for t in fixed) or "(ничего)",
            flexible="\n".join(f"{t.id}: {t.title}" + (f" — {t.notes}" if t.notes else "") for t in flexible),
            earliest=f"{to_local(window_start, tz):%H:%M}",
            latest=f"{to_local(window_end, tz):%H:%M}",
        )
        answer = await self._llm.chat([Message("user", prompt)], [])
        try:
            proposal = extract_json(answer.content)
        except ValueError:
            log.warning("user %s: unparsable plan: %r", user.id, answer.content[:300])
            proposal = {}

        plan.items, plan.skipped = schedule(proposal, flexible, fixed, day, tz, now)
        plan.comment = str(proposal.get("comment") or "").strip()[:300]
        if plan.items:
            payload = json.dumps([{"task_id": i.task.id, "at": i.at.isoformat(), "minutes": i.minutes}
                                  for i in plan.items])
            plan.id = (await self._repo.save_day_plan(user.id, day.isoformat(), payload)).id
        return plan


async def apply_plan(repo: Repository, user: User, plan_id: int) -> int | None:
    """Ставит дела на время из плана (с напоминаниями). Возвращает число перенесённых или None, если плана нет."""
    plan = await repo.get_day_plan(user.id, plan_id)
    if plan is None:
        return None
    if plan.applied_at is not None:
        return 0
    moved = 0
    for item in json.loads(plan.items):
        task = await repo.get_task(user.id, item["task_id"])
        if task is not None and task.status == TaskStatus.OPEN:
            await repo.update_task(user.id, task.id, due_at=datetime.fromisoformat(item["at"]))
            moved += 1
    await repo.mark_plan_applied(plan_id)
    return moved
