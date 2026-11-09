import html
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from aid.agent.agent import Agent
from aid.config import Settings
from aid.core.models import utcnow
from aid.core.repository import Repository
from aid.core.timeutils import format_local
from aid.telegram.keyboards import TaskAction, created_kb, task_card

log = logging.getLogger(__name__)


def make_router(agent: Agent, repo: Repository, settings: Settings) -> Router:
    router = Router()

    async def ensure_user(msg_or_cb):
        tg_user = msg_or_cb.from_user
        chat = msg_or_cb.chat if isinstance(msg_or_cb, Message) else msg_or_cb.message.chat
        return await repo.get_or_create_user(tg_user.id, chat.id, settings.default_timezone)

    @router.message(CommandStart())
    async def start(message: Message):
        user = await ensure_user(message)
        await message.answer(
            "Привет! Пиши мне дела обычным текстом — я запишу и напомню.\n"
            "Например: «завтра в 10 позвонить в банк», «каждый вторник в 19 спортзал».\n\n"
            f"/tasks — список дел\n/reset — забыть контекст диалога\n\nЧасовой пояс: {user.timezone}"
        )

    @router.message(Command("tasks"))
    async def tasks(message: Message):
        user = await ensure_user(message)
        items = await repo.list_open_tasks(user.id)
        if not items:
            await message.answer("Открытых дел нет 🎉")
            return
        lines = []
        for t in items:
            due = f" — {format_local(t.due_at, user.timezone)}" if t.due_at else ""
            rep = " 🔁" if t.rrule else ""
            lines.append(f"• {html.escape(t.title)}{due}{rep}")
        await message.answer("\n".join(lines))

    @router.message(Command("reset"))
    async def reset(message: Message):
        agent.history.clear(message.from_user.id)
        await message.answer("Контекст диалога очищен.")

    @router.message(F.text)
    async def on_text(message: Message):
        user = await ensure_user(message)
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            reply = await agent.handle(user, message.text)
        except Exception:
            log.exception("agent failed")
            await message.answer("Что-то пошло не так с моделью, попробуй ещё раз.")
            return
        await message.answer(html.escape(reply.text))
        # дедуп: одна задача могла быть затронута несколько раз за ход
        seen = {}
        for t in reply.touched:
            seen[t.id] = t
        for t in seen.values():
            if t.status == "open":
                await message.answer(task_card(t, user.timezone), reply_markup=created_kb(t))

    @router.callback_query(TaskAction.filter())
    async def on_action(cb: CallbackQuery, callback_data: TaskAction):
        user = await ensure_user(cb)
        tid = callback_data.task_id
        if callback_data.action == "done":
            task = await repo.complete_task(user.id, tid, user.timezone)
            if task and task.status == "open":  # повторяющаяся — перенеслась
                note = f"✅ Сделано. Следующий раз: {format_local(task.due_at, user.timezone)}"
            else:
                note = "✅ Сделано"
        elif callback_data.action == "cancel":
            task = await repo.cancel_task(user.id, tid)
            note = "🗑 Удалено"
        elif callback_data.action == "snooze":
            until = utcnow() + timedelta(minutes=callback_data.minutes)
            task = await repo.snooze_task(user.id, tid, until)
            note = f"⏰ Напомню {format_local(until, user.timezone)}"
        else:
            task, note = None, ""

        if task is None:
            await cb.answer("Задача не найдена или уже закрыта", show_alert=False)
            return
        await cb.message.edit_text(f"{task_card(task, user.timezone)}\n\n{note}", reply_markup=None)
        await cb.answer()

    return router
