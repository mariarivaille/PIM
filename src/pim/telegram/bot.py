from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, TelegramObject

from pim.agent.agent import Agent
from pim.config import Settings
from pim.core.models import Task, User
from pim.core.repository import Repository
from pim.reminders.digest import Digest
from pim.reminders.review import Review
from pim.stt.base import Transcriber
from pim.telegram.handlers import make_router
from pim.telegram.keyboards import format_digest, reminder_kb, review_messages, task_card


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

    async def send_review(self, user: User, review: Review) -> None:
        for text, kb in review_messages(review, user.timezone):
            await self._bot.send_message(user.chat_id, text, reply_markup=kb)


COMMANDS = [
    BotCommand(command="start", description="Начать и посмотреть настройки"),
    BotCommand(command="tasks", description="Список дел"),
    BotCommand(command="lists", description="Списки: покупки, в поездку…"),
    BotCommand(command="digest", description="План на сегодня"),
    BotCommand(command="review", description="Разбор: что переносим?"),
    BotCommand(command="memory", description="Что я о тебе знаю"),
    BotCommand(command="reset", description="Забыть контекст разговора"),
]


async def setup_commands(bot: Bot) -> None:
    """Меню команд в Telegram — из кода, чтобы не расходилось с обработчиками.

    Имя, about и description бота задаются вручную в @BotFather и здесь не трогаются.
    """
    await bot.set_my_commands(COMMANDS)


def make_bot(settings: Settings) -> Bot:
    return Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_dispatcher(agent: Agent, repo: Repository, settings: Settings,
                    transcriber: Transcriber | None = None) -> Dispatcher:
    dp = Dispatcher()
    allowlist = AllowlistMiddleware(settings.allowed_user_ids)
    dp.message.outer_middleware(allowlist)
    dp.callback_query.outer_middleware(allowlist)
    dp.include_router(make_router(agent, repo, settings, transcriber))
    return dp
