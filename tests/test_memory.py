import asyncio
from datetime import timedelta

import pytest

from pim.agent.agent import Agent, History
from pim.agent.commands import parse_delete_facts
from pim.agent.tools import ToolContext, run_tool
from pim.core.models import MemoryKind, MemorySource, utcnow
from pim.core.timeutils import to_local
from pim.llm.base import Message, ToolCall
from pim.telegram.keyboards import memory_list

from tests.conftest import TZ
from tests.test_agent import ScriptedLLM


async def test_remember_update_forget(repo, user):
    ctx = ToolContext(repo, user, user_text="запомни")
    res = await run_tool(ctx, "remember", {"content": "Мама — Ольга, ДР 12 марта", "kind": "person"})
    mid = res["remembered"]["number"]
    assert [m.content for m in ctx.remembered] == ["Мама — Ольга, ДР 12 марта"]

    await run_tool(ctx, "update_memory", {"number": mid, "content": "Мама — Ольга Петровна, ДР 12 марта"})
    [m] = await repo.list_memories(user.id)
    assert m.content == "Мама — Ольга Петровна, ДР 12 марта" and m.kind == MemoryKind.PERSON

    assert (await run_tool(ctx, "forget", {"number": mid}))["forgotten"].startswith("Мама")
    assert await repo.list_memories(user.id) == []


async def test_memory_tool_validation(repo, user):
    ctx = ToolContext(repo, user, user_text="запомни")
    assert "error" in await run_tool(ctx, "remember", {"content": "  "})
    assert "error" in await run_tool(ctx, "remember", {"content": "x", "kind": "friend"})
    assert "error" in await run_tool(ctx, "remember", {"content": "x", "until_date": "в пятницу"})
    assert "error" in await run_tool(ctx, "forget", {"number": 999})
    assert "error" in await run_tool(ctx, "update_memory", {"number": 999, "content": "y"})


async def test_temporary_memory_expires(repo, user):
    ctx = ToolContext(repo, user, user_text="запомни")
    await run_tool(ctx, "remember", {"content": "в отпуске", "until_day": "today"})
    [m] = await repo.list_memories(user.id)
    local = to_local(m.expires_at, TZ)
    assert (local.hour, local.minute) == (23, 59)
    assert await repo.list_memories(user.id, now=m.expires_at + timedelta(seconds=1)) == []


async def test_memories_are_isolated_between_users(repo, user):
    other = await repo.get_or_create_user(2, 200, TZ)
    mine = await repo.add_memory(user.id, "мой секрет")
    assert await repo.list_memories(other.id) == []
    assert await repo.get_memory(other.id, mine.number) is None
    assert await repo.delete_memory(other.id, mine.number) is None
    assert "error" in await run_tool(ToolContext(repo, other), "forget", {"number": mine.number})
    assert [m.content for m in await repo.list_memories(user.id)] == ["мой секрет"]


async def test_memories_go_to_system_prompt_and_reply(repo, user):
    await repo.add_memory(user.id, "Мама — Ольга", MemoryKind.PERSON)
    llm = ScriptedLLM([
        Message("assistant", tool_calls=[ToolCall("c1", "remember",
                                                  {"content": "Спортзал по вт и чт", "kind": "routine"})]),
        Message("assistant", "Запомнил."),
    ])
    agent = Agent(llm, repo, History(repo, 10))
    reply = await agent.handle(user, "запомни, что я хожу в спортзал по вт и чт")
    system = llm.calls[0][0].content
    assert "Мама — Ольга" in system
    assert "Спортзал по вт и чт" in [m.content for m in await repo.list_memories(user.id)]
    assert not hasattr(reply, "remembered")  # память скрытая: агент не отдаёт её наружу для показа


async def test_user_messages_are_processed_in_order(repo, user):
    order = []

    class SlowLLM:
        async def chat(self, messages, tools):
            text = messages[-1].content
            order.append(f"start {text}")
            await asyncio.sleep(0.05)
            order.append(f"end {text}")
            return Message("assistant", "ok")

    agent = Agent(SlowLLM(), repo, History(repo, 10))
    other = await repo.get_or_create_user(2, 200, TZ)
    await asyncio.gather(agent.handle(user, "a"), agent.handle(user, "b"), agent.handle(other, "c"))
    # у одного пользователя — строго по очереди
    assert order.index("end a") < order.index("start b")
    # другой пользователь не ждёт первого
    assert order.index("start c") < order.index("end a")


def test_memory_list_rendering(user):
    class M:
        def __init__(self, number, kind, content, expires_at=None, source=MemorySource.EXPLICIT):
            self.number, self.kind, self.content, self.expires_at = number, kind, content, expires_at
            self.source = source

    text = memory_list([
        M(1, MemoryKind.FACT, "работаю из дома по пт"),
        M(2, MemoryKind.PERSON, "мама <Ольга>"),
        M(5, MemoryKind.FACT, "в отпуске", utcnow() + timedelta(days=2), MemorySource.INFERRED),
    ], TZ)
    assert text.index("Люди") < text.index("Прочее")
    assert "2. мама &lt;Ольга&gt;" in text and "5. в отпуске" in text and "(до " in text
    assert "удали факт" in text and "5. в отпуске" in text and "(сам)" in text
    assert "запоминаю сам" in text and "только по просьбе" in memory_list([], TZ, auto_memory=False)
    assert "ничего" in memory_list([], TZ)


async def test_remember_requires_explicit_request(repo, user):
    for text in ["я хожу в спортзал по вт и чт", "напомни завтра позвонить маме"]:
        res = await run_tool(ToolContext(repo, user, user_text=text), "remember", {"content": "x"})
        # автозапоминание включено: не переспрашивать, рефлексия подберёт сама
        assert "error" in res and "система запомнит сама" in res["error"]
    user.auto_memory = False
    res = await run_tool(ToolContext(repo, user, user_text="я сова"), "remember", {"content": "x"})
    assert "Запомнить это?" in res["error"]
    user.auto_memory = True
    for text in ["Запомни: мама — Ольга", "учти, что я сова", "имей ввиду, я в отпуске", "знай, что я веган",
                 "не забывай, что у меня аллергия"]:
        assert "remembered" in await run_tool(ToolContext(repo, user, user_text=text), "remember", {"content": text})
    assert len(await repo.list_memories(user.id)) == 5


async def test_yes_after_offer_counts_as_request(repo, user):
    offer = "Записал. Запомнить это?"
    ok = ToolContext(repo, user, user_text="да, давай", prev_assistant=offer)
    assert "remembered" in await run_tool(ok, "remember", {"content": "спортзал вт/чт"})
    # «да» без предложения — не просьба; и предложение без согласия — тоже
    assert "error" in await run_tool(ToolContext(repo, user, user_text="да", prev_assistant="Готово."),
                                     "remember", {"content": "x"})
    assert "error" in await run_tool(ToolContext(repo, user, user_text="нет, не надо", prev_assistant=offer),
                                     "remember", {"content": "x"})


async def test_numbers_are_per_user_and_stable(repo, user):
    other = await repo.get_or_create_user(2, 200, TZ)
    a = await repo.add_memory(user.id, "a")
    b = await repo.add_memory(user.id, "b")
    x = await repo.add_memory(other.id, "x")
    assert (a.number, b.number, x.number) == (1, 2, 1)
    await repo.delete_memory(user.id, 1)
    c = await repo.add_memory(user.id, "c")
    assert c.number == 3  # номер удалённого не переиспользуется
    assert [m.number for m in await repo.list_memories(user.id)] == [2, 3]


@pytest.mark.parametrize("text, expected", [
    ("удали факт 3", [3]),
    ("Удали факт №3.", [3]),
    ("удали факты 3, 5 и 7", [3, 5, 7]),
    ("забудь факт 2", [2]),
    ("удалить факт #4", [4]),
    ("удали факт про маму", None),
    ("удали задачу 3", None),
    ("напомни удалить факт 3 из презентации", None),
])
def test_parse_delete_facts(text, expected):
    assert parse_delete_facts(text) == expected


async def test_delete_facts_command_skips_llm(repo, user):
    await repo.add_memory(user.id, "мама — Ольга")
    await repo.add_memory(user.id, "в отпуске")

    class NoLLM:
        async def chat(self, messages, tools):
            raise AssertionError("удаление по номеру не должно ходить в LLM")

    agent = Agent(NoLLM(), repo, History(repo, 10))
    reply = await agent.handle(user, "удали факты 2 и 9")
    assert reply.text == "🗑 Забыл факт 2: в отпуске\nФакта 9 нет в памяти"
    assert [m.content for m in await repo.list_memories(user.id)] == ["мама — Ольга"]
