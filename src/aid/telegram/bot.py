from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject

from aid.agent.agent import Agent
from aid.config import Settings
from aid.core.models import Task, User
from aid.core.repository import Repository
from aid.telegram.handlers import make_router
from aid.reminders.digest import Digest
from aid.telegram.keyboards import format_digest, reminder_kb, task_card


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, allowed: list[int]):
        self._allowed = set(allowed)

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if self._allowed and (user is None or user.id not in self._allowed):
            return None
        return await handler(event, data)


class TelegramNotifier:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send_reminder(self, user: User, task: Task) -> None:
        await self._bot.send_message(
            user.chat_id, "🔔 Напоминаю\n\n" + task_card(task, user.timezone), reply_markup=reminder_kb(task)
        )

    async def send_digest(self, user: User, digest: Digest) -> None:
        await self._bot.send_message(user.chat_id, format_digest(digest, user.timezone))


def make_bot(settings: Settings) -> Bot:
    return Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_dispatcher(agent: Agent, repo: Repository, settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    allowlist = AllowlistMiddleware(settings.allowed_user_ids)
    dp.message.outer_middleware(allowlist)
    dp.callback_query.outer_middleware(allowlist)
    dp.include_router(make_router(agent, repo, settings))
    return dp
