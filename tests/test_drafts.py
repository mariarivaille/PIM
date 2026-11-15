from pim.agent.agent import DRAFT_MARK, Agent, History, wrongly_refused
from pim.agent.tools import ToolContext, run_tool
from pim.llm.base import Message, ToolCall

from tests.test_agent import ScriptedLLM

CONGRATS = "Мам, с днём рождения! Спасибо тебе за всё ❤️"


async def test_draft_is_shown_separately_and_kept_in_history(repo, user):
    llm = ScriptedLLM([
        Message("assistant", tool_calls=[ToolCall("c1", "show_draft", {"text": CONGRATS, "to": "мама"})]),
        Message("assistant", "Вот черновик. Поправить что-нибудь?"),
    ])
    agent = Agent(llm, repo, History(repo, 10))
    reply = await agent.handle(user, "напиши поздравление маме")
    assert reply.drafts == [CONGRATS] and CONGRATS not in reply.text
    # для «сделай короче» модель должна видеть сам текст
    last = (await agent.history.get(user.id))[-1]
    assert DRAFT_MARK in last.content and CONGRATS in last.content


async def test_draft_validation(repo, user):
    ctx = ToolContext(repo, user)
    assert "error" in await run_tool(ctx, "show_draft", {"text": "  "})
    assert "error" in await run_tool(ctx, "show_draft", {"text": "x" * 5000})
    assert ctx.drafts == []


def test_refusal_retry_for_drafts_but_not_code():
    refusal = "С этим я не помогаю — я веду твои дела, напоминания, списки и планы."
    assert wrongly_refused(refusal, "составь письмо в УК")
    assert wrongly_refused(refusal, "напиши поздравление маме")
    assert not wrongly_refused(refusal, "напиши функцию сортировки на питоне")
    assert not wrongly_refused(refusal, "напиши сочинение про осень")


async def test_generic_draft_asks_for_personal_details_once(repo, user):
    await repo.add_memory(user.id, "Мама — Ольга, любит театр и садоводство")
    ctx = ToolContext(repo, user)
    generic = {"text": "С днём рождения! Желаю здоровья и счастья!", "to": "мама"}
    res = await run_tool(ctx, "show_draft", generic)
    assert "театр" in res["error"]
    assert "shown" in await run_tool(ctx, "show_draft", generic)  # второй раз не мучаем
    personal = {"text": "Мам, с днём рождения! Пусть на даче всё цветёт, а в театре — только премьеры", "to": "маме"}
    assert "shown" in await run_tool(ToolContext(repo, user), "show_draft", personal)


async def test_draft_typed_into_reply_is_caught(repo, user):
    llm = ScriptedLLM([Message("assistant", f"{DRAFT_MARK}\nМам, с днём рождения!")])
    reply = await Agent(llm, repo, History(repo, 10)).handle(user, "сделай короче")
    assert reply.drafts == ["Мам, с днём рождения!"] and DRAFT_MARK not in reply.text
