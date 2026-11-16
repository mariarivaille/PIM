import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, Message, TelegramObject, WebAppInfo

from pim.agent.agent import Agent
from pim.config import Settings
from pim.core.models import ItemList, ListItem, Task, User, utcnow
from pim.core.timeutils import humanize_until
from pim.core.calendar import Occurrence
from pim.core.repository import Repository
from pim.reminders.digest import Digest
from pim.reminders.dates import UpcomingDate
from pim.reminders.review import Review
from pim.stt.base import Transcriber
from pim.telegram.bot_helpers import invite_code
from pim.telegram.cards import remember_card, remove_cards
from pim.telegram.handlers import make_router
from pim.telegram.keyboards import event_reminder_text
from pim.telegram.keyboards import (
    assignment_kb,
    assignment_text,
    list_additions_text,
    open_list_kb,
    outcome_text,
    date_alert_kb,
    date_alert_text,
    format_digest,
    reminder_kb,
    review_messages,
    task_card,
)


log = logging.getLogger(__name__)


class AllowlistMiddleware(BaseMiddleware):
    """Пускает: тех, кто в ALLOWED_USER_IDS; уже присоединившихся по приглашению; и пришедших с живым приглашением."""

    def __init__(self, allowed: list[int], repo: Repository):
        self._allowed = set(allowed)
        self._repo = repo

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        _log_incoming(event, user)
        if not self._allowed or (user is not None and await self._is_allowed(user.id, event)):
            return await handler(event, data)
        log.info("blocked update from %s (not in allowlist, no invite)", user.id if user else None)
        return None

    async def _is_allowed(self, user_id: int, event: TelegramObject) -> bool:
        if user_id in self._allowed or await self._repo.get_user(user_id) is not None:
            return True
        text = getattr(event, "text", None) or ""
        code = invite_code(text)
        return code is not None and await self._repo.valid_invite(code) is not None




class TelegramNotifier:
    def __init__(self, bot: Bot, repo: Repository | None = None):
        self._bot = bot
        self._repo = repo

    async def send_reminder(self, user: User, task: Task, usual_snooze: int | None = None) -> None:
        if self._repo:  # прошлые карточки этого дела (повторяющееся — вчерашнее напоминание) больше не нужны
            await remove_cards(self._bot, self._repo, task.id)
        now = utcnow()
        if task.remind_at and task.due_at and task.due_at > now:
            # напоминание заранее: главное — сколько осталось до самого дела
            header = f"🔔 {humanize_until(task.due_at, now, user.timezone).capitalize()} — пора собираться"
        else:
            header = "🔔 Напоминаю"
        sent = await self._bot.send_message(
            user.chat_id, header + "\n\n" + task_card(task, user.timezone, show_remind=False),
            reply_markup=reminder_kb(task, usual_snooze),
        )
        if self._repo:
            await remember_card(self._repo, task.id, sent)

    async def send_digest(self, user: User, digest: Digest) -> None:
        await self._bot.send_message(user.chat_id, format_digest(digest, user.timezone))

    async def send_event_reminder(self, user: User, occurrence: Occurrence) -> None:
        await self._bot.send_message(user.chat_id, event_reminder_text(occurrence, user.timezone, utcnow()))

    async def send_date_alert(self, user: User, upcoming: UpcomingDate) -> None:
        await self._bot.send_message(user.chat_id, date_alert_text(upcoming), reply_markup=date_alert_kb(upcoming))

    async def send_assignment(self, assignee: User, task: Task, by_name: str) -> None:
        await self._bot.send_message(assignee.chat_id, assignment_text(task, by_name, assignee.timezone),
                                     reply_markup=assignment_kb(task))

    async def send_outcome(self, by: User, task: Task, assignee_name: str) -> None:
        await self._bot.send_message(by.chat_id, outcome_text(task, assignee_name))

    async def send_list_additions(self, recipient: User, lst: ItemList, adder_name: str,
                                  items: list[ListItem]) -> None:
        await self._bot.send_message(recipient.chat_id, list_additions_text(lst, adder_name, items),
                                     reply_markup=open_list_kb(lst))

    async def send_review(self, user: User, review: Review) -> None:
        for text, kb in review_messages(review, user.timezone):
            await self._bot.send_message(user.chat_id, text, reply_markup=kb)



COMMANDS = [
    BotCommand(command="start", description="Начать и посмотреть настройки"),
    BotCommand(command="tasks", description="Список дел"),
    BotCommand(command="app", description="Открыть Pim: дела, календарь, память"),
    BotCommand(command="calendar", description="Календарь: все события"),
    BotCommand(command="lists", description="Списки: покупки, в поездку…"),
    BotCommand(command="digest", description="План на сегодня"),
    BotCommand(command="review", description="Разбор дня: несделанное — на завтра"),
    BotCommand(command="memory", description="Что я о тебе знаю"),
    BotCommand(command="habits", description="Твои привычки по времени"),
    BotCommand(command="people", description="Близкие: поручения и общие списки"),
    BotCommand(command="invite", description="Пригласить близкого"),
    BotCommand(command="reset", description="Забыть контекст разговора"),
]


async def setup_commands(bot: Bot) -> None:
    """Меню команд в Telegram — из кода, чтобы не расходилось с обработчиками.

    Имя, about и description бота задаются вручную в @BotFather и здесь не трогаются.
    """
    await bot.set_my_commands(COMMANDS)


async def setup_menu_button(bot: Bot, webapp_url: str) -> None:
    """Кнопка «Pim» слева от поля ввода — открывает Mini App (дела, календарь, память)."""
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Pim", web_app=WebAppInfo(url=webapp_url)))


def _log_incoming(event: TelegramObject, user) -> None:
    """Что пришло от Telegram — без текста сообщения, только тип: для разбора «бот не реагирует»."""
    if isinstance(event, Message):
        origin = event.forward_origin
        log.info("incoming message from %s: type=%s forwarded=%s has_text=%s reply=%s quote=%s",
                 user.id if user else None, event.content_type, origin.type if origin else None,
                 bool(event.text or event.caption), event.reply_to_message is not None, event.quote is not None)


def make_bot(settings: Settings) -> Bot:
    return Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_dispatcher(agent: Agent, repo: Repository, settings: Settings,
                    transcriber: Transcriber | None = None) -> Dispatcher:
    dp = Dispatcher()
    allowlist = AllowlistMiddleware(settings.allowed_user_ids, repo)
    dp.message.outer_middleware(allowlist)
    dp.callback_query.outer_middleware(allowlist)
    dp.include_router(make_router(agent, repo, settings, transcriber))
    return dp
