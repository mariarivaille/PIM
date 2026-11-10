from datetime import timedelta

from pim.agent.agent import Agent, History
from pim.agent.tools import ToolContext, run_tool
from pim.core.models import TaskStatus, utcnow
from pim.llm.base import Message, ToolCall
from pim.llm.gigachat import GigaChatProvider
from pim.reminders.scheduler import ReminderScheduler

from tests.conftest import TZ


class ScriptedLLM:
    """Отдаёт заранее заданные ответы и запоминает, что ему прислали."""

    def __init__(self, answers: list[Message]):
        self._answers = list(answers)
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools):
        self.calls.append(list(messages))
        return self._answers.pop(0)


async def test_agent_creates_task_via_tool(repo, user):
    llm = ScriptedLLM([
        Message("assistant", tool_calls=[ToolCall("c1", "create_task",
                                                  {"title": "позвонить маме", "due_at": "2030-01-10T15:00"})]),
        Message("assistant", "Записал: позвонить маме, чт 10.01 15:00"),
    ])
    agent = Agent(llm, repo, History(repo, 10))
    reply = await agent.handle(user, "в четверг 10 января в 15 позвонить маме")

    assert reply.text.startswith("Записал")
    assert [t.title for t in reply.touched] == ["позвонить маме"]
    tasks = await repo.list_open_tasks(user.id)
    assert len(tasks) == 1 and tasks[0].due_at.hour == 12  # 15:00 MSK = 12:00 UTC
    # результат инструмента ушёл модели во втором запросе
    assert llm.calls[1][-1].role == "tool"
    # история хранит только текст
    assert [m.role for m in await agent.history.get(user.id)] == ["user", "assistant"]


async def test_history_expires_after_ttl(repo, user):
    start = utcnow()
    now = [start]
    h = History(repo, 10, ttl_seconds=60, clock=lambda: now[0])
    await h.add(user.id, Message("user", "a"), Message("assistant", "b"))
    now[0] = start + timedelta(seconds=59)
    assert len(await h.get(user.id)) == 2
    await h.add(user.id, Message("user", "c"))  # активность продлевает жизнь истории
    now[0] = start + timedelta(seconds=59 + 61)
    assert await h.get(user.id) == []
    await h.add(user.id, Message("user", "d"))
    # после паузы в контекст попадает только свежая реплика — старые остаются в БД, но не мешают
    assert [m.content for m in await h.get(user.id)][-1] == "d"


async def test_history_survives_restart_and_reset(repo, user):
    await History(repo, 10).add(user.id, Message("user", "привет"), Message("assistant", "привет!"))
    fresh = History(repo, 10)  # новый объект = перезапуск бота
    assert [m.content for m in await fresh.get(user.id)] == ["привет", "привет!"]
    await fresh.clear(user.id)
    assert await fresh.get(user.id) == []
    await fresh.add(user.id, Message("user", "новое"))
    assert [m.content for m in await fresh.get(user.id)] == ["новое"]


async def test_tool_errors_are_returned_to_model(repo, user):
    ctx = ToolContext(repo, user)
    assert "error" in await run_tool(ctx, "create_task", {"title": "x", "due_at": "завтра"})
    assert "error" in await run_tool(ctx, "create_task", {"title": "x", "rrule": "FREQ=DAILY"})
    assert "error" in await run_tool(ctx, "complete_task", {"task_id": 999})
    assert "error" in await run_tool(ctx, "create_task", {"bogus": 1})
    assert "error" in await run_tool(ctx, "set_timezone", {"timezone": "Mars/Base"})
    ok = await run_tool(ctx, "create_task", {"title": "x", "due_at": "2030-01-01T09:00", "rrule": "RRULE:FREQ=DAILY"})
    assert ok["created"]["rrule"] == "FREQ=DAILY"


async def test_scheduler_sends_once(repo, user):
    from datetime import timedelta
    from pim.core.models import utcnow

    sent = []

    class FakeNotifier:
        async def send_reminder(self, u, t, usual_snooze=None):
            sent.append(t.id)

    t = await repo.create_task(user.id, "x", due_at=utcnow() - timedelta(seconds=1))
    sched = ReminderScheduler(repo, FakeNotifier(), 30)
    assert await sched.tick() == 1
    assert await sched.tick() == 0
    assert sent == [t.id]
    assert (await repo.get_task(user.id, t.id)).status == TaskStatus.OPEN


def test_gigachat_message_conversion():
    call = Message("assistant", tool_calls=[ToolCall("x", "list_tasks", {})],
                   provider_meta={"functions_state_id": "abc"})
    api = GigaChatProvider._to_api(call)
    assert api.function_call.name == "list_tasks"
    assert api.functions_state_id == "abc"

    result = GigaChatProvider._to_api(Message("tool", '{"tasks": []}', tool_call_id="x", name="list_tasks"))
    assert result.role == "function" and result.name == "list_tasks"


async def test_gigachat_retries_network_errors_once(monkeypatch):
    import ssl

    async def no_sleep(*_):
        return None

    monkeypatch.setattr("pim.llm.gigachat.asyncio.sleep", no_sleep)
    provider = GigaChatProvider(credentials="x", scope="s", model="m", verify_ssl=False)
    calls = []

    async def flaky(payload):
        calls.append(1)
        if len(calls) == 1:
            raise ssl.SSLError("bad record mac")
        return "ok"

    provider._client.achat = flaky
    assert await provider._achat_with_retry(None) == "ok" and len(calls) == 2


def test_looks_like_code():
    from pim.agent.agent import looks_like_code
    assert looks_like_code("Вот пример:\n```python\nprint(1)\n```")
    assert looks_like_code("def bubble(a):\n    for i in range(len(a)):\n        pass")
    assert looks_like_code("for (let i = 0; i < n; i++) {\n  swap(a, i)\n}")
    assert not looks_like_code("Записал: позвонить маме, чт 10.01 15:00")
    assert not looks_like_code("Вот твои задачи:\n1. Купить хлеб — 19:00\n2. Забрать посылку — 19:00")


async def test_code_reply_is_replaced(repo, user):
    from pim.agent.agent import OFF_TOPIC_REPLY
    llm = ScriptedLLM([Message("assistant", "Конечно!\n```python\ndef sort(a):\n    return sorted(a)\n```")])
    reply = await Agent(llm, repo, History(repo, 10)).handle(user, "Сортировка пузырьком")
    assert reply.text == OFF_TOPIC_REPLY
    # и в историю попадает отказ, а не код
    assert (await History(repo, 10).get(user.id))[-1].content == OFF_TOPIC_REPLY
