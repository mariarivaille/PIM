import html
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from pim.agent.agent import Agent
from pim.config import Settings
from pim.core.models import utcnow
from pim.core.repository import Repository
from pim.core.timeutils import format_local
from pim.reminders.digest import build_digest
from pim.stt.base import Transcriber
from pim.telegram.keyboards import (
    TaskAction,
    created_kb,
    format_digest,
    memory_list,
    task_card,
)

log = logging.getLogger(__name__)


def forward_sender(origin, message: Message) -> str:
    """Кто автор пересланного сообщения — для контекста модели («Созвон с Андреем»)."""
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return "тебя самого" if message.from_user and user.id == message.from_user.id else user.full_name
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    name = getattr(origin, "sender_user_name", None) or (chat.title if chat else None)
    signature = getattr(origin, "author_signature", None)
    if name and signature:
        return f"{signature} ({name})"
    return name or "неизвестного отправителя"


def make_router(agent: Agent, repo: Repository, settings: Settings, transcriber: Transcriber | None) -> Router:
    router = Router()

    async def ensure_user(msg_or_cb):
        tg_user = msg_or_cb.from_user
        chat = msg_or_cb.chat if isinstance(msg_or_cb, Message) else msg_or_cb.message.chat
        return await repo.get_or_create_user(tg_user.id, chat.id, settings.default_timezone)

    @router.message(CommandStart())
    async def start(message: Message):
        user = await ensure_user(message)
        digest = f"в {user.digest_time}" if user.digest_time else "выключена"
        await message.answer(
            "Привет! Я Pim. Пиши или наговаривай мне дела — я запишу и напомню.\n"
            "Например: «завтра в 10 позвонить в банк», «каждый вторник в 19 спортзал».\n\n"
            "Запоминаю важное о тебе: сам из разговоров или по просьбе — «запомни, что мама — Ольга, ДР 12 марта». "
            "Посмотреть — /memory, удалить — «удали факт 3».\n\n"
            "/tasks — список дел\n/digest — сводка на сегодня\n/memory — что я о тебе знаю\n"
            "/reset — забыть контекст диалога\n\n"
            f"Часовой пояс: {user.timezone}\nУтренняя сводка: {digest} "
            "(«присылай сводку в 8:30» / «не присылай сводку»)"
        )

    @router.message(Command("digest"))
    async def digest(message: Message):
        user = await ensure_user(message)
        d = await build_digest(repo, user, utcnow())
        await message.answer(format_digest(d, user.timezone))

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
        await ensure_user(message)
        await agent.history.clear(message.from_user.id)
        await message.answer("Контекст диалога очищен. Память о тебе и дела не тронуты.")

    @router.message(Command("memory"))
    async def memory(message: Message):
        user = await ensure_user(message)
        await message.answer(memory_list(await repo.list_memories(user.id), user.timezone, user.auto_memory))

    @router.message(F.text | F.caption)
    async def on_text(message: Message):
        # подпись к фото/файлу обрабатываем как текст — так работают и пересланные посты с картинкой
        user = await ensure_user(message)
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            await process(message, user, message.text or message.caption)

    @router.message(F.voice | F.audio | F.video_note)
    async def on_voice(message: Message):
        if transcriber is None:
            await message.answer("Голосовые выключены, напиши текстом.")
            return
        media = message.voice or message.audio or message.video_note
        if media.duration and media.duration > settings.max_voice_seconds:
            await message.answer(f"Слишком длинное, я разбираю до {settings.max_voice_seconds // 60} мин.")
            return
        user = await ensure_user(message)
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            try:
                audio = await message.bot.download(media)
                text = await transcriber.transcribe(audio.getvalue())
            except Exception:
                log.exception("transcription failed")
                await message.answer("Не получилось распознать голосовое, попробуй ещё раз или напиши текстом.")
                return
            if not text:
                await message.answer("Не расслышал, повтори, пожалуйста.")
                return
            await message.answer(f"🎙 <i>{html.escape(text)}</i>")
            await process(message, user, text)

    @router.message(F.forward_origin)
    async def on_forwarded_without_text(message: Message):
        await message.answer("В пересланном сообщении я понимаю текст, подписи и голосовые — здесь их нет. "
                             "Напиши, что с ним сделать.")

    async def process(message: Message, user, text: str):
        try:
            if origin := message.forward_origin:
                reply = await agent.handle_forwarded(user, text, forward_sender(origin, message), origin.date)
            else:
                reply = await agent.handle(user, text)
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
                await message.answer(task_card(t, user.timezone, auto_time=t.id in reply.auto_time),
                                     reply_markup=created_kb(t))

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
