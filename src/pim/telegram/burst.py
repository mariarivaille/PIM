"""Склейка сообщений, пришедших подряд.

«Переслать с подписью» Telegram присылает двумя сообщениями: сначала подпись пользователя, следом пересланное
(альбом — ещё и по сообщению на фото). Обработанные по отдельности, они теряют смысл: подпись без контекста,
пересланное без инструкции. Поэтому сообщения одного пользователя копятся, пока между ними меньше window
секунд, и уходят на обработку пачкой.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pim.llm.base import Image

log = logging.getLogger(__name__)

BURST_WINDOW = 1.2


@dataclass
class Incoming:
    message: Any  # aiogram Message — откуда пришло и куда отвечать
    text: str
    images: list[Image] = field(default_factory=list)
    forwarded: bool = False


class Burster:
    def __init__(self, flush: Callable[[int, list[Incoming]], Awaitable[None]], window: float = BURST_WINDOW):
        self._flush = flush
        self._window = window
        self._items: dict[int, list[Incoming]] = {}
        self._timers: dict[int, asyncio.Task] = {}

    def add(self, user_id: int, item: Incoming) -> None:
        self._items.setdefault(user_id, []).append(item)
        if timer := self._timers.get(user_id):
            timer.cancel()  # пачка ещё не закрыта — ждём дальше
        self._timers[user_id] = asyncio.create_task(self._flush_later(user_id))

    async def _flush_later(self, user_id: int) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return
        # таймер снимаем до обработки: новое сообщение начнёт новую пачку и не прервёт эту
        if self._timers.get(user_id) is asyncio.current_task():
            del self._timers[user_id]
        items = self._items.pop(user_id, [])
        if not items:
            return
        try:
            await self._flush(user_id, items)
        except Exception:
            log.exception("failed to process messages of user %s", user_id)
