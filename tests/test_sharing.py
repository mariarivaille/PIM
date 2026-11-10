from datetime import timedelta
from types import SimpleNamespace

import pytest

from pim.agent.tools import ToolContext, run_tool
from pim.core.models import TaskStatus, utcnow
from pim.core.timeutils import to_local
from pim.reminders.scheduler import ReminderScheduler
from pim.sharing.people import find_person
from pim.telegram.bot import AllowlistMiddleware
from pim.telegram.bot_helpers import invite_code
from pim.telegram.keyboards import assignment_text, list_additions_text, outcome_text, people_text

from tests.conftest import TZ


@pytest.fixture
async def pair(repo, user):
    """Мария (user) пригласила Андрея — они связаны."""
    await repo.get_or_create_user(user.id, 100, TZ, name="Мария")
    andrey = await repo.get_or_create_user(2, 200, TZ, name="Андрей Петров")
    code = await repo.create_invite(user.id)
    assert await repo.accept_invite(code, andrey.id) == user.id
    return await repo.get_user(user.id), andrey


async def test_invite_links_both_ways_once(repo, user, pair):
    maria, andrey = pair
    assert [(c.alias, p.id) for c, p in await repo.connections(maria.id)] == [("Андрей Петров", andrey.id)]
    assert [(c.alias, p.id) for c, p in await repo.connections(andrey.id)] == [("Мария", maria.id)]

    code = await repo.create_invite(maria.id)
    assert await repo.accept_invite(code, maria.id) is None  # себя пригласить нельзя
    olga = await repo.get_or_create_user(3, 300, TZ, name="Ольга")
    assert await repo.accept_invite(code, olga.id) == maria.id
    assert await repo.accept_invite(code, 4) is None  # одноразовое

    old = await repo.create_invite(maria.id)
    assert await repo.valid_invite(old, now=utcnow() + timedelta(days=8)) is None  # протухло


def test_invite_code():
    assert invite_code("/start join_abc123") == "abc123"
    assert invite_code("/start") is None and invite_code("/start other") is None and invite_code("привет") is None


def test_find_person():
    def p(alias, name=None):
        return SimpleNamespace(alias=alias), SimpleNamespace(name=name or alias)

    people = [p("Андрей Петров"), p("Мама", "Ольга"), p("Савва")]
    for name, expected in [("Андрей", "Андрей Петров"), ("Андрею", "Андрей Петров"), ("андрея петрова", "Андрей Петров"),
                           ("маме", "Мама"), ("Ольге", "Мама"), ("Савве", "Савва"), ("Игорю", None)]:
        found = find_person(people, name)
        assert (found[0].alias if found else None) == expected, name
    assert find_person([p("Александр"), p("Александра")], "Алекс") is None  # неоднозначно


async def test_shared_list(repo, user, pair):
    maria, andrey = pair
    m_ctx, a_ctx = ToolContext(repo, maria), ToolContext(repo, andrey)
    await run_tool(m_ctx, "add_to_list", {"list": "покупки", "items": "молоко, хлеб"})
    assert "error" in await run_tool(m_ctx, "share_list", {"list": "покупки", "person": "Игорь"})
    res = await run_tool(m_ctx, "share_list", {"list": "покупки", "person": "Андрею"})
    assert res == {"shared_list": "Покупки", "with": "Андрей Петров", "already_shared": False}

    # Андрей видит, отмечает и дополняет
    [(lst, _, _)] = await repo.user_lists(andrey.id)
    assert lst.id in await repo.shared_list_ids(andrey.id) and lst.id in await repo.shared_list_ids(maria.id)
    await run_tool(a_ctx, "check_off", {"list": "покупки", "items": "молоко"})
    await run_tool(a_ctx, "add_to_list", {"list": "покупки", "items": "сыр"})
    assert [(i.text, i.done) for i in await repo.list_items(lst.id)] == [("хлеб", False), ("сыр", False),
                                                                       ("молоко", True)]
    # удалить и переподелиться может только владелец
    assert "error" in await run_tool(a_ctx, "delete_list", {"list": "покупки"})
    assert "error" in await run_tool(a_ctx, "share_list", {"list": "покупки", "person": "Мария"})

    # Марии сообщат только про «сыр» — пункты, лежавшие до шаринга, не всплывают
    [(g_lst, adder, items)] = await repo.pending_list_additions()
    assert (g_lst.id, adder, [i.text for i in items]) == (lst.id, andrey.id, ["сыр"])


async def test_lists_of_others_stay_private(repo, user, pair):
    maria, andrey = pair
    await run_tool(ToolContext(repo, maria), "add_to_list", {"list": "подарки Андрею", "items": "часы"})
    assert await repo.user_lists(andrey.id) == []


async def test_assign_task_and_outcome(repo, user, pair):
    maria, andrey = pair
    res = await run_tool(ToolContext(repo, maria), "assign_task",
                         {"person": "Андрею", "title": "купить хлеб", "day": "tomorrow", "time": "19:00"})
    assert res["assigned_to"] == "Андрей Петров" and res["due"].endswith("19:00")
    [task] = await repo.list_open_tasks(andrey.id)
    assert task.assigned_by == maria.id and task.notes == "Просьба от Мария"
    assert to_local(task.due_at, TZ).hour == 19
    assert await repo.list_open_tasks(maria.id) == []

    sent = []

    class Notifier:
        async def send_assignment(self, assignee, t, by_name):
            sent.append(("assign", assignee.name, by_name, t.title))

        async def send_outcome(self, by, t, assignee_name):
            sent.append(("outcome", by.name, assignee_name, t.status))

        async def send_list_additions(self, *a):
            pass

    sched = ReminderScheduler(repo, Notifier(), 30)
    await sched.tick_shared()
    await sched.tick_shared()
    assert sent == [("assign", "Андрей Петров", "Мария", "купить хлеб")]

    await repo.complete_task(andrey.id, task.id, TZ)
    await sched.tick_shared()
    await sched.tick_shared()
    assert sent[1:] == [("outcome", "Мария", "Андрей Петров", TaskStatus.DONE)]


async def test_assign_requires_connection(repo, user):
    res = await run_tool(ToolContext(repo, user), "assign_task", {"person": "Андрей", "title": "x"})
    assert "/invite" in res["error"]


async def test_allowlist_middleware(repo, user, pair):
    maria, andrey = pair
    mw = AllowlistMiddleware([maria.id], repo)
    passed = []

    async def handler(event, data):
        passed.append(data["event_from_user"].id)

    async def call(uid, text=""):
        await mw(handler, SimpleNamespace(text=text), {"event_from_user": SimpleNamespace(id=uid)})

    await call(maria.id)                    # в ALLOWED_USER_IDS
    await call(andrey.id)                   # уже присоединился по приглашению
    await call(999)                         # чужой — нет
    code = await repo.create_invite(maria.id)
    await call(998, f"/start join_{code}")  # с живым приглашением — да
    await call(997, "/start join_fake")     # с выдуманным — нет
    assert passed == [maria.id, andrey.id, 998]


def test_texts():
    task = SimpleNamespace(id=1, title="купить хлеб", due_at=None, rrule=None, notes="Просьба от Мария",
                           status=TaskStatus.DONE)
    assert assignment_text(task, "Мария", TZ).startswith("📨 <b>Мария просит:</b>")
    assert outcome_text(task, "Андрей") == "✅ Андрей: «купить хлеб» — сделано"
    task.status = TaskStatus.CANCELLED
    assert outcome_text(task, "Андрей").startswith("🗑")
    lst = SimpleNamespace(id=1, name="Покупки")
    items = [SimpleNamespace(text="сыр"), SimpleNamespace(text="хлеб")]
    assert list_additions_text(lst, "Андрей", items) == "🛒 Андрей добавляет в «Покупки»: сыр, хлеб"
    assert "/invite" in people_text([])


async def test_create_task_asks_whose_task_when_close_person_mentioned(repo, user, pair):
    maria, andrey = pair
    ctx = ToolContext(repo, maria, user_text="Андрей должен позвать меня на кофе в обед")
    res = await run_tool(ctx, "create_task", {"title": "позвать меня на кофе", "day": "today", "time": "13:00"})
    assert "assign_task" in res["error"] and "Андрей Петров" in res["error"]
    # если модель настаивает (это и правда дело пользователя) — второй раз не мешаем
    res = await run_tool(ctx, "create_task", {"title": "позвонить Андрею", "day": "today", "time": "13:00"})
    assert "created" in res
    # без упоминаний близких — сразу
    ctx = ToolContext(repo, maria, user_text="купить хлеб в 19")
    assert "created" in await run_tool(ctx, "create_task", {"title": "купить хлеб", "day": "today", "time": "19:00"})


async def test_rename_and_display_names(repo, user, pair):
    maria, andrey = pair
    assert await repo.display_name(andrey.id, maria.id) == "Мария"
    m_ctx = ToolContext(repo, maria)
    await run_tool(m_ctx, "call_person", {"person": "Андрей", "new_name": "Андрюша"})
    await run_tool(m_ctx, "show_me_as", {"person": "Андрюше", "my_name": "Маша"})
    assert [c.alias for c, _ in await repo.connections(maria.id)] == ["Андрюша"]
    assert await repo.display_name(andrey.id, maria.id) == "Маша"
    # поручение подписано так, как исполнитель зовёт поручившего
    await run_tool(m_ctx, "assign_task", {"person": "Андрюша", "title": "купить хлеб"})
    [task] = await repo.list_open_tasks(andrey.id)
    assert task.notes == "Просьба от Маша"


async def test_assignment_wording_is_from_assignee_view(repo, user, pair):
    maria, andrey = pair
    ctx = ToolContext(repo, maria)
    res = await run_tool(ctx, "assign_task", {"person": "Андрей", "title": "позвать меня на кофе"})
    assert "Мария" in res["error"]
    assert "assigned_to" in await run_tool(ctx, "assign_task", {"person": "Андрей", "title": "позвать Марию на кофе"})


async def test_accept_decline_assignment(repo, user, pair):
    maria, andrey = pair
    ctx = ToolContext(repo, maria)
    await run_tool(ctx, "assign_task", {"person": "Андрей", "title": "купить хлеб"})
    await run_tool(ctx, "assign_task", {"person": "Андрей", "title": "помыть машину"})
    bread, car = await repo.list_open_tasks(andrey.id)
    assert bread.assign_state == "pending"

    assert await repo.set_assign_state(maria.id, bread.id, "accepted") is None  # чужое поручение не трогаем
    assert (await repo.set_assign_state(andrey.id, bread.id, "accepted")).assign_state == "accepted"
    declined = await repo.set_assign_state(andrey.id, car.id, "declined")
    assert declined.status == TaskStatus.CANCELLED
    # об отказе поручившему сообщили кнопкой — второго «отменено» от планировщика не будет
    assert all(t.id != car.id for t, _, _ in await repo.pending_outcomes())


async def test_adjustment_changes_the_assigned_task(repo, user, pair):
    from pim.agent.agent import Agent, History
    from pim.llm.base import Message, ToolCall
    from tests.test_agent import ScriptedLLM

    maria, andrey = pair
    await run_tool(ToolContext(repo, maria), "assign_task",
                   {"person": "Андрей", "title": "позвать Марию на кофе", "day": "today", "time": "13:00"})
    [task] = await repo.list_open_tasks(andrey.id)
    llm = ScriptedLLM([
        Message("assistant", tool_calls=[ToolCall("c1", "update_task",
                                                  {"task_id": task.id, "day": "thu", "time": "13:00"})]),
        Message("assistant", "Перенёс на четверг 13:00."),
    ])
    agent = Agent(llm, repo, History(repo, 10))
    await agent.handle_adjustment(andrey, task, "давай в четверг")
    assert llm.calls[0][-1].content.startswith(f"[Корректировка поручения: дело task_id={task.id}")
    after = await repo.get_task(andrey.id, task.id)
    assert to_local(after.due_at, TZ).weekday() == 3 and to_local(after.due_at, TZ).hour == 13


def test_assign_reply_texts():
    from pim.telegram.keyboards import adjusted_text, assign_reply_text, assignment_kb
    task = SimpleNamespace(id=5, title="позвать Марию на кофе", due_at=None)
    assert assign_reply_text(task, "Андрей", "accept") == "👍 Андрей принимает: «позвать Марию на кофе»"
    assert assign_reply_text(task, "Андрей", "decline").startswith("✖️ Андрей отклоняет")
    assert [b.text for row in assignment_kb(task).inline_keyboard for b in row] == [
        "✅ Принять", "✖️ Отклонить", "✏️ Скорректировать"]
    text = adjusted_text("Андрей", "позвать Марию на кофе", "вт 10.11 13:00", task, TZ)
    assert "срок: вт 10.11 13:00 → без срока" in text and "было «" not in text
