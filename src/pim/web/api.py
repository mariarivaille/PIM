"""API для Telegram Mini App: дела, календарь и память — те же данные, что видит бот.

Работает в одном процессе с ботом (pim.main) и ходит в БД через тот же Repository. Даты и подписи
(«пн 16.11», «19:00–20:30», «через 2 ч») считает сервер, а не браузер: так они совпадают с тем, что пишет бот.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from pim.agent.tools import lead_text
from pim.core import calendar as cal
from pim.core.models import Event, Memory, Task, TaskStatus, User, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import WEEKDAYS_RU, format_local, humanize_until, to_local
from pim.web.auth import InvalidInitData, validate_init_data

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
CALENDAR_DAYS = 60
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
WEEKDAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MEMORY_SECTIONS = [("person", "Люди"), ("routine", "Распорядок и привычки"), ("preference", "Предпочтения"),
                   ("fact", "Прочее")]


def _day_title(d: date, today: date) -> str:
    if d == today:
        return "Сегодня"
    if d == today + timedelta(days=1):
        return "Завтра"
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def _task_group(task: Task, today: date, tz: str) -> str:
    if task.due_at is None:
        return "nodate"
    d = to_local(task.due_at, tz).date()
    if task.due_at <= utcnow() and d < today:
        return "overdue"
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return "later"


def task_json(task: Task, tz: str, today: date, n: int, by_name: str | None = None) -> dict[str, Any]:
    now = utcnow()
    out: dict[str, Any] = {"id": task.id, "n": n, "title": task.title, "notes": task.notes,
                           "group": _task_group(task, today, tz), "recurring": bool(task.rrule), "from": by_name}
    if task.due_at:
        local = to_local(task.due_at, tz)
        out |= {"due": format_local(task.due_at, tz), "time": f"{local:%H:%M}", "date": local.date().isoformat(),
                "in": humanize_until(task.due_at, now, tz), "passed": task.due_at <= now}
        if task.remind_at:
            out["remind"] = f"напомню в {to_local(task.remind_at, tz):%H:%M} — за {lead_text(task.due_at - task.remind_at)}"
    return out


def _event_time(occ: cal.Occurrence, tz: str) -> str:
    if occ.event.all_day:
        return "Весь день"
    start, end = to_local(occ.start, tz), to_local(occ.end, tz)
    tail = f"{end:%H:%M}" if end.date() == start.date() else f"{WEEKDAYS_RU[end.weekday()]} {end:%d.%m %H:%M}"
    return f"{start:%H:%M}–{tail}"


def event_json(occ: cal.Occurrence, tz: str) -> dict[str, Any]:
    e = occ.event
    out: dict[str, Any] = {"kind": "event", "id": e.id, "title": e.title, "time": _event_time(occ, tz),
                           "sort": "00:00" if e.all_day else f"{to_local(occ.start, tz):%H:%M}",
                           "location": e.location, "notes": e.notes, "recurring": bool(e.rrule)}
    if e.remind_before and not e.all_day:
        out["remind"] = f"напомню за {lead_text(timedelta(minutes=e.remind_before))}"
    return out


def _all_day_count(e: Event, tz: str) -> int:
    return max(1, (to_local(e.ends_at, tz).date() - to_local(e.starts_at, tz).date()).days) if e.all_day else 0


def memory_json(m: Memory, tz: str) -> dict[str, Any]:
    return {"number": m.number, "kind": str(m.kind.value if hasattr(m.kind, "value") else m.kind),
            "content": m.content, "until": format_local(m.expires_at, tz) if m.expires_at else None}


def create_app(repo: Repository, bot_token: str, on_task_changed=None) -> FastAPI:
    """on_task_changed(task_id) — убрать из чата карточки дела, закрытого в Mini App (telegram/cards.py)."""
    app = FastAPI(title="Pim", docs_url=None, redoc_url=None, openapi_url=None)

    async def current_user(authorization: str = Header(default="")) -> User:
        scheme, _, init_data = authorization.partition(" ")
        if scheme.lower() != "tma" or not init_data:
            raise HTTPException(401, "нужно открыть из Telegram")
        try:
            tg_user = validate_init_data(init_data, bot_token)
        except InvalidInitData as e:
            raise HTTPException(401, f"подпись Telegram не прошла проверку: {e}") from e
        user = await repo.get_user(tg_user["id"])
        if user is None:
            raise HTTPException(403, "сначала напиши боту")
        return user

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/state")
    async def state(user: User = Depends(current_user)):
        tz = user.timezone
        now = utcnow()
        today = to_local(now, tz).date()
        names = {}
        tasks = await repo.list_open_tasks(user.id)
        for t in tasks:
            if t.assigned_by and t.assigned_by not in names:
                names[t.assigned_by] = await repo.display_name(user.id, t.assigned_by)

        day_start = to_local(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        events = await repo.list_events(user.id)
        window = cal.expand(events, day_start, day_start + timedelta(days=CALENDAR_DAYS), tz)
        days: dict[str, dict] = {}

        def day_entry(d: date) -> dict:
            key = d.isoformat()
            if key not in days:
                days[key] = {"date": key, "title": _day_title(d, today), "short": f"{WEEKDAYS_RU[d.weekday()]}",
                             "num": d.day, "items": []}
            return days[key]

        periods = []
        for occ in window:
            if _all_day_count(occ.event, tz) > 1:
                start = to_local(occ.start, tz).date()
                n = _all_day_count(occ.event, tz)
                last = start + timedelta(days=n - 1)
                periods.append({"id": occ.event.id, "title": occ.event.title, "days": n,
                                "span": f"{WEEKDAYS_RU[start.weekday()]} {start:%d.%m} – "
                                        f"{WEEKDAYS_RU[last.weekday()]} {last:%d.%m}",
                                "now": start <= today <= last, "from": start.isoformat(), "to": last.isoformat()})
                continue
            day_entry(to_local(occ.start, tz).date())["items"].append(event_json(occ, tz))
        for t in tasks:
            if t.due_at and day_start <= t.due_at < day_start + timedelta(days=CALENDAR_DAYS):
                local = to_local(t.due_at, tz)
                day_entry(local.date())["items"].append(
                    {"kind": "task", "id": t.id, "title": t.title, "time": f"{local:%H:%M}", "sort": f"{local:%H:%M}",
                     "notes": t.notes, "recurring": bool(t.rrule)})
        for d in days.values():
            d["items"].sort(key=lambda i: (i["sort"], i["kind"] != "event"))

        strip = []
        for i in range(14):
            d = today + timedelta(days=i)
            entry = days.get(d.isoformat())
            strip.append({"date": d.isoformat(), "short": WEEKDAYS_RU[d.weekday()], "num": d.day,
                          "count": len(entry["items"]) if entry else 0, "weekend": d.weekday() >= 5})
        recurring = []
        for e in events:
            if e.rrule:
                occ = cal.Occurrence(e, e.starts_at, e.ends_at)
                recurring.append({"id": e.id, "title": e.title, "rule": cal.rrule_text(e, tz),
                                  "time": _event_time(occ, tz)})

        return {
            "user": {"name": user.name, "timezone": tz, "today": today.isoformat()},
            "tasks": [task_json(t, tz, today, i, names.get(t.assigned_by)) for i, t in enumerate(tasks, 1)],
            "strip": strip,
            "days": [days[k] for k in sorted(days)],
            "periods": periods,
            "recurring": recurring,
        }

    @app.get("/api/memory")
    async def memory(user: User = Depends(current_user)):
        items = await repo.list_memories(user.id)
        return {"auto": user.auto_memory,
                "sections": [{"kind": kind, "title": title,
                              "items": [memory_json(m, user.timezone) for m in items
                                        if memory_json(m, user.timezone)["kind"] == kind]}
                             for kind, title in MEMORY_SECTIONS]}

    @app.post("/api/tasks/{task_id}/done")
    async def done(task_id: int, user: User = Depends(current_user)):
        task = await repo.complete_task(user.id, task_id, user.timezone)
        if task is None:
            raise HTTPException(404, "дело не найдено или уже закрыто")
        if on_task_changed:
            await on_task_changed(task.id)
        nxt = format_local(task.due_at, user.timezone) if task.status == TaskStatus.OPEN and task.due_at else None
        return {"ok": True, "next": nxt}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: int, user: User = Depends(current_user)):
        task = await repo.get_task(user.id, task_id)
        if task is None or task.status != TaskStatus.OPEN:
            raise HTTPException(404, "дело не найдено или уже закрыто")
        await repo.cancel_task(user.id, task_id)
        if on_task_changed:
            await on_task_changed(task_id)
        return {"ok": True}

    @app.delete("/api/events/{event_id}")
    async def delete_event(event_id: int, user: User = Depends(current_user)):
        event = await repo.get_event(user.id, event_id)
        if event is None:
            raise HTTPException(404, "события уже нет")
        await repo.update_event(user.id, event_id, cancelled=True, next_reminder_at=None)
        return {"ok": True}

    @app.delete("/api/memory/{number}")
    async def forget(number: int, user: User = Depends(current_user)):
        if await repo.delete_memory(user.id, number) is None:
            raise HTTPException(404, "такого факта нет")
        return {"ok": True}

    return app


async def serve(app: FastAPI, host: str, port: int) -> None:
    """uvicorn внутри event loop бота; сигналы остаются за aiogram — он и завершает процесс."""
    import contextlib

    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    await _Server(uvicorn.Config(app, host=host, port=port, log_level="warning", proxy_headers=True,
                                 forwarded_allow_ips="127.0.0.1")).serve()
