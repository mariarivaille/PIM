import asyncio
from datetime import timedelta

from pim.agent.agent import Agent, ForwardedPart, History
from pim.agent.tools import ToolContext, memory_requested
from pim.core.models import utcnow
from pim.llm.base import Message
from pim.telegram.burst import Burster, Incoming

from tests.test_agent import ScriptedLLM


async def test_burst_merges_quick_messages_per_user():
    flushed = []

    async def flush(user_id, items):
        flushed.append((user_id, [i.text for i in items]))

    b = Burster(flush, window=0.05)
    b.add(1, Incoming(None, "мне подготовить это"))
    b.add(2, Incoming(None, "чужое"))
    await asyncio.sleep(0.02)
    b.add(1, Incoming(None, "пересланный пост", forwarded=True))
    await asyncio.sleep(0.15)
    assert sorted(flushed) == [(1, ["мне подготовить это", "пересланный пост"]), (2, ["чужое"])]

    b.add(1, Incoming(None, "позже"))  # после паузы — новая пачка
    await asyncio.sleep(0.15)
    assert flushed[-1] == (1, ["позже"])


async def test_new_message_does_not_interrupt_running_flush():
    done = []

    async def slow_flush(user_id, items):
        await asyncio.sleep(0.1)
        done.append([i.text for i in items])

    b = Burster(slow_flush, window=0.02)
    b.add(1, Incoming(None, "a"))
    await asyncio.sleep(0.05)  # пачка «a» уже обрабатывается
    b.add(1, Incoming(None, "b"))
    await asyncio.sleep(0.3)
    assert done == [["a"], ["b"]]


async def test_forwarded_batch_with_comment(repo, user):
    llm = ScriptedLLM([Message("assistant", "Записал: подготовить заявку.")])
    agent = Agent(llm, repo, History(repo, 10))
    sent = utcnow() - timedelta(days=7)
    await agent.handle_forwarded_batch(user, [ForwardedPart("Открыт набор на программу по безопасности ИИ", "Reddit",
                                                            sent)], comment="мне подготовить это")
    text = llm.calls[0][-1].content
    assert text.startswith("[Пересланное сообщение от Reddit, отправлено")
    assert "Открыт набор на программу" in text
    assert text.endswith("[Комментарий пользователя к пересланному] мне подготовить это")


def test_memory_request_only_from_users_own_comment(repo, user):
    forwarded = "[Пересланное сообщение от Ольга, отправлено пн]\nзапомни мой номер"
    assert not memory_requested(ToolContext(repo, user, user_text=forwarded))
    with_comment = forwarded + "\n\n[Комментарий пользователя к пересланному] запомни это"
    assert memory_requested(ToolContext(repo, user, user_text=with_comment))
    harmless = forwarded + "\n\n[Комментарий пользователя к пересланному] что это?"
    assert not memory_requested(ToolContext(repo, user, user_text=harmless))
