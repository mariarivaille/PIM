from aid.agent.agent import Agent, History
from aid.agent.tools import ToolContext, run_tool
from aid.core.models import TaskStatus
from aid.llm.base import Message, ToolCall
from aid.llm.gigachat import GigaChatProvider
from aid.reminders.scheduler import ReminderScheduler

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
    agent = Agent(llm, repo, History(10))
    reply = await agent.handle(user, "в четверг 10 января в 15 позвонить маме")

    assert reply.text.startswith("Записал")
    assert [t.title for t in reply.touched] == ["позвонить маме"]
    tasks = await repo.list_open_tasks(user.id)
    assert len(tasks) == 1 and tasks[0].due_at.hour == 12  # 15:00 MSK = 12:00 UTC
    # результат инструмента ушёл модели во втором запросе
    assert llm.calls[1][-1].role == "tool"
    # история хранит только текст
    assert [m.role for m in agent.history.get(user.id)] == ["user", "assistant"]


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
    from aid.core.models import utcnow

    sent = []

    class FakeNotifier:
        async def send_reminder(self, u, t):
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
