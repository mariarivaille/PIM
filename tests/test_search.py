from pim.agent.agent import Agent, History
from pim.agent.tools import ToolContext, run_tool
from pim.llm.base import Message, ToolCall
from pim.search.web import SearchResult
from pim.telegram.format import md_to_html

from tests.test_agent import ScriptedLLM


class FakeSearch:
    def __init__(self):
        self.queries = []

    async def search(self, query, today):
        self.queries.append(query)
        return SearchResult("МФЦ на Ленина, 24А: пн–пт 8–19, сб 9–17", [{"title": "spr.ru", "url": "https://x.ru"}])


async def test_web_search_tool(repo, user):
    search = FakeSearch()
    ctx = ToolContext(repo, user, searcher=search, searches_left=1)
    res = await run_tool(ctx, "web_search", {"query": "МФЦ Ленина Екатеринбург часы работы"})
    assert "пн–пт 8–19" in res["answer"] and res["sources"][0]["url"] == "https://x.ru"
    assert "лимит" in (await run_tool(ctx, "web_search", {"query": "ещё"}))["error"]  # платно — не больше лимита
    assert "выключен" in (await run_tool(ToolContext(repo, user), "web_search", {"query": "x"}))["error"]


async def test_agent_searches_and_answers(repo, user):
    search = FakeSearch()
    llm = ScriptedLLM([
        Message("assistant", tool_calls=[ToolCall("c1", "web_search", {"query": "МФЦ Ленина часы работы"})]),
        Message("assistant", "Пн–пт 8–19, сб 9–17 ([spr.ru](https://x.ru))"),
    ])
    reply = await Agent(llm, repo, History(repo, 10), searcher=search).handle(user, "во сколько работает МФЦ на Ленина?")
    assert search.queries == ["МФЦ Ленина часы работы"] and "8–19" in reply.text


def test_md_to_html():
    out = md_to_html("**Ленина, 24А** <пн>:\n* пн–пт 8–19\n([spr.ru](https://x.ru/a?b=1&c=2))")
    assert "<b>Ленина, 24А</b>" in out and "&lt;пн&gt;" in out and "• пн–пт" in out
    assert '<a href="https://x.ru/a?b=1&amp;c=2">spr.ru</a>' in out


def test_city_from_memory_goes_to_prompt():
    from types import SimpleNamespace
    from pim.agent.agent import location_hint
    mem = [SimpleNamespace(content="Живёт в Екатеринбурге"), SimpleNamespace(content="Кот — Барсик")]
    assert location_hint(mem) == "«Живёт в Екатеринбурге»"


def test_no_credits_message():
    from types import SimpleNamespace
    from pim.telegram.handlers import llm_error_text
    assert "кредиты" in llm_error_text(SimpleNamespace(status_code=402), "x")
    assert "Ключ OpenRouter" in llm_error_text(SimpleNamespace(status_code=401), "x")
    assert llm_error_text(ValueError(), "x") == "x"
