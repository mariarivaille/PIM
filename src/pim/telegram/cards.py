"""Копии карточки дела в чате (ответ при создании, напоминание…): когда дело закрыто или изменено,
старые копии с кнопками удаляются, чтобы не жать «Сделано» под уже закрытым делом."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from pim.core.repository import Repository

log = logging.getLogger(__name__)


async def delete_message(bot: Bot, chat_id: int, message_id: int) -> None:
    """Удалить сообщение; если Telegram не даёт (старше 48 ч) — хотя бы снять кнопки."""
    try:
        await bot.delete_message(chat_id, message_id)
        return
    except TelegramAPIError:
        pass
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError:
        log.debug("could not clean up message %s in chat %s", message_id, chat_id)


async def remember_card(repo: Repository, task_id: int, message: Message) -> None:
    await repo.add_task_card(task_id, message.chat.id, message.message_id)


async def remove_cards(bot: Bot, repo: Repository, task_id: int, keep_message_id: int | None = None) -> None:
    """Удаляет все копии карточки дела, кроме keep_message_id (сообщение, под которым нажали кнопку)."""
    for card in await repo.pop_task_cards(task_id):
        if card.message_id != keep_message_id:
            await delete_message(bot, card.chat_id, card.message_id)
