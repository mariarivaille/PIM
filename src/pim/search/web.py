"""Поиск в интернете для агента: «во сколько работает МФЦ на Ленина?», «найди рецепт без мяса».

Ищет OpenRouter (плагин web): модель получает свежие результаты поиска и отвечает по ним со ссылками.
Это отдельный вызов, а не режим основной модели: искать нужно только тогда, когда агент сам решил,
что без интернета не ответить, — каждый поиск платный.
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

SEARCH_PROMPT = """\
Найди в интернете ответ на запрос пользователя и ответь по-русски кратко и по делу, только фактами из найденного:
адреса, часы работы, телефоны, цены, шаги, ингредиенты. Если источники расходятся или данные могут устареть —
так и скажи. Не выдумывай: если не нашёл — напиши, что не нашёл. Сегодня {today}."""


@dataclass
class SearchResult:
    answer: str
    sources: list[dict] = field(default_factory=list)  # {"title", "url"}


class WebSearch(Protocol):
    async def search(self, query: str, today: str) -> SearchResult: ...


class OpenRouterSearch:
    def __init__(self, api_key: str, model: str, base_url: str, max_results: int = 3, max_tokens: int = 1000):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_results = max_results
        self._max_tokens = max_tokens

    async def search(self, query: str, today: str) -> SearchResult:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": SEARCH_PROMPT.format(today=today)},
                      {"role": "user", "content": query}],
            max_tokens=self._max_tokens,
            temperature=0.2,
            extra_body={"plugins": [{"id": "web", "max_results": self._max_results}]},
        )
        msg = resp.choices[0].message
        sources, seen = [], set()
        for ann in getattr(msg, "annotations", None) or []:
            cite = ann.get("url_citation") if isinstance(ann, dict) else getattr(ann, "url_citation", None)
            if isinstance(cite, dict):
                url, title = cite.get("url"), cite.get("title")
            else:
                url, title = getattr(cite, "url", None), getattr(cite, "title", None)
            if url and url not in seen:
                seen.add(url)
                sources.append({"title": (title or url)[:120], "url": url})
        return SearchResult(answer=(msg.content or "").strip(), sources=sources)


def make_search(settings) -> WebSearch | None:
    if settings.web_search == "off" or not settings.openrouter_api_key:
        return None
    return OpenRouterSearch(settings.openrouter_api_key, settings.web_search_model or settings.openrouter_model,
                            settings.openrouter_base_url, max_results=settings.web_search_results)
