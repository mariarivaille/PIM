import json
from datetime import date, timedelta
from types import SimpleNamespace

from pim.agent.tools import ToolContext, run_tool
from pim.core.models import MemoryKind
from pim.core.timeutils import parse_local, to_local
from pim.llm.base import Message
from pim.planning.day import DayPlanner, Plan, apply_plan, gather, schedule
from pim.telegram.keyboards import plan_message

from tests.conftest import TZ
from tests.test_agent import ScriptedLLM

DAY = date(2026, 11, 11)
MORNING = parse_local("2026-11-11T07:00", TZ)


def hhmm(dt):
    return to_local(dt, TZ).strftime("%H:%M")


def task(id, title="x", due=None):
    return SimpleNamespace(id=id, title=title, due_at=due, notes=None)


def test_schedule_validates_and_resolves_conflicts():
    fixed = [task(1, "созвон", parse_local("2026-11-11T15:00", TZ))]
    flexible = [task(i) for i in range(10, 16)]
    proposal = {"plan": [
        {"task_id": 10, "time": "15:10", "minutes": 30},   # пересекается с созвоном → после него
        {"task_id": 11, "time": "15:20", "minutes": 60},   # пересекается и с 10 → ещё позже
        {"task_id": 12, "time": "06:00", "minutes": 20},   # до начала окна → 8:00
        {"task_id": 13, "time": "21:45", "minutes": 60},   # вылезает за 22:00 → не влезло
        {"task_id": 14, "time": "утром"},                  # непонятное время → не влезло
        {"task_id": 99, "time": "12:00"},                  # чужой id — игнор
    ], "skip": [15]}
    items, skipped = schedule(proposal, flexible, fixed, DAY, TZ, MORNING)
    assert [(i.task.id, hhmm(i.at), i.minutes) for i in items] == [(12, "08:00", 20), (10, "15:30", 30),
                                                                  (11, "16:00", 60)]
    assert sorted(t.id for t in skipped) == [13, 14, 15]


def test_schedule_today_starts_after_now():
    now = parse_local("2026-11-11T13:07", TZ)
    items, _ = schedule({"plan": [{"task_id": 1, "time": "10:00", "minutes": 30}]}, [task(1)], [], DAY, TZ, now)
    assert hhmm(items[0].at) == "13:30"  # «сейчас + 15 минут», с округлением до четверти часа


async def test_gather(repo, user):
    await repo.create_task(user.id, "сегодня в 15", due_at=parse_local("2026-11-11T15:00", TZ))
    await repo.create_task(user.id, "просрочено", due_at=parse_local("2026-11-09T10:00", TZ))
    await repo.create_task(user.id, "без срока")
    await repo.create_task(user.id, "завтра", due_at=parse_local("2026-11-12T10:00", TZ))
    fixed, flexible = await gather(repo, user, DAY)
    assert [t.title for t in fixed] == ["сегодня в 15"]
    assert [t.title for t in flexible] == ["просрочено", "без срока"]


async def test_plan_and_apply(repo, user):
    await repo.add_memory(user.id, "Встаёт около 10, работает с 11 до 19", MemoryKind.ROUTINE)
    await repo.create_task(user.id, "созвон", due_at=parse_local("2026-11-11T15:00", TZ))
    bank = await repo.create_task(user.id, "позвонить в банк")
    shop = await repo.create_task(user.id, "купить продукты")
    llm = ScriptedLLM([Message("assistant", json.dumps({
        "plan": [{"task_id": bank.id, "time": "11:30", "minutes": 20},
                 {"task_id": shop.id, "time": "19:30", "minutes": 45}],
        "skip": [], "comment": "Банк до созвона, продукты после работы.",
    }, ensure_ascii=False))])
    plan = await DayPlanner(llm, repo).plan(user, DAY, now=MORNING)

    prompt = llm.calls[0][0].content
    assert "работает с 11 до 19" in prompt and "15:00 созвон" in prompt and f"{bank.id}: позвонить в банк" in prompt
    assert [(i.task.title, hhmm(i.at)) for i in plan.items] == [("позвонить в банк", "11:30"), ("купить продукты", "19:30")]
    assert plan.id is not None

    # до подтверждения ничего не меняется
    assert (await repo.get_task(user.id, bank.id)).due_at is None
    other = await repo.get_or_create_user(2, 200, TZ)
    assert await apply_plan(repo, other, plan.id) is None
    assert await apply_plan(repo, user, plan.id) == 2
    assert hhmm((await repo.get_task(user.id, bank.id)).due_at) == "11:30"
    assert await apply_plan(repo, user, plan.id) == 0  # повторное нажатие ничего не делает


async def test_nothing_to_plan_skips_llm(repo, user):
    class NoLLM:
        async def chat(self, *a):
            raise AssertionError("незачем звать модель")

    await repo.create_task(user.id, "сегодня в 15", due_at=parse_local("2026-11-11T15:00", TZ))
    plan = await DayPlanner(NoLLM(), repo).plan(user, DAY, now=MORNING)
    assert plan.empty and not plan.skipped
    text, kb = plan_message(plan, TZ)
    assert "Планировать нечего" in text and kb is None


def test_plan_message():
    plan = Plan(day=DAY, fixed=[task(1, "созвон", parse_local("2026-11-11T15:00", TZ))], comment="Ок!", id=7)
    plan.items = [SimpleNamespace(task=task(2, "банк"), at=parse_local("2026-11-11T11:30", TZ), minutes=20)]
    plan.skipped = [task(3, "шторы")]
    text, kb = plan_message(plan, TZ)
    assert "План на ср 11.11" in text and "15:00 созвон" in text and "11:30–11:50 банк" in text
    assert "Не влезло" in text and "шторы" in text
    assert [b.text for row in kb.inline_keyboard for b in row] == ["✅ Поставить напоминания", "✖️ Не надо"]


async def test_plan_day_tool(repo, user):
    class FakePlanner:
        async def plan(self, u, day):
            return Plan(day=day)

    ctx = ToolContext(repo, user, planner=FakePlanner())
    res = await run_tool(ctx, "plan_day", {"day": "tomorrow"})
    assert ctx.plan is not None and "Планировать нечего" in res["note"]
    assert "error" in await run_tool(ToolContext(repo, user), "plan_day", {})


def test_schedule_respects_wake_and_busy_from_memory():
    proposal = {
        "wake": "10:00",
        "busy": [{"from": "11:00", "to": "19:00", "what": "работа"}, {"from": "19:30", "to": "21:00", "what": "зал"}],
        "plan": [{"task_id": 1, "time": "09:00", "minutes": 20},   # до подъёма → 10:00 (сдвиг 1 ч — можно)
                 {"task_id": 3, "time": "18:45", "minutes": 30},   # конец работы → 19:00
                 {"task_id": 2, "time": "19:10", "minutes": 60},   # дальше только после зала (21:00) → не влезло
                 {"task_id": 5, "time": "12:00", "minutes": 30},   # в рабочее время → сдвиг на 7 ч → не влезло
                 {"task_id": 4, "time": "21:30", "minutes": 60}],  # после 22:00 не влезает
    }
    items, skipped = schedule(proposal, [task(i) for i in range(1, 6)], [], DAY, TZ, MORNING)
    assert [(i.task.id, hhmm(i.at)) for i in items] == [(1, "10:00"), (3, "19:00")]
    assert sorted(t.id for t in skipped) == [2, 4, 5]


def test_facts_for_weekday():
    from pim.planning.day import facts_for_weekday
    facts = ["Ходит в спортзал по вт и чт вечером", "В четверг забирает сына из школы", "По будням работает с 11",
             "По выходным ездит на дачу", "Мама — Ольга", "Встаёт в 10"]
    assert facts_for_weekday(facts, 3) == facts[:3]          # четверг
    assert facts_for_weekday(facts, 5) == ["По выходным ездит на дачу"]  # суббота
    assert facts_for_weekday(facts, 0) == ["По будням работает с 11"]    # понедельник
