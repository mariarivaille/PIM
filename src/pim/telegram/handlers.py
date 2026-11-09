import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from pim.agent.agent import Agent
from pim.config import Settings
from pim.core.models import utcnow
from pim.core.repository import Repository
from pim.core.timeutils import format_local, to_local
from pim.learning.timing import load_profile
from pim.llm.base import Image
from pim.reminders.digest import build_digest
from pim.reminders.dates import CONGRATS_OFFSET, GIFT_OFFSET, UpcomingDate, reminder_at
from pim.reminders.review import build_review, postpone_all_to_tomorrow, postpone_to_tomorrow
from pim.stt.base import Transcriber
from pim.telegram.keyboards import (
    DateAction,
    ListAction,
    ReviewAction,
    TaskAction,
    DATE_CONGRATS,
    DATE_GIFT,
    created_kb,
    date_alert_kb,
    format_digest,
    habits_text,
    list_card,
    lists_menu,
    memory_list,
    review_messages,
    task_card,
)

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024


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
        review_at = f"в {user.review_time}" if user.review_time else "выключен"
        await message.answer(
            "Привет! Я Pim. Пиши или наговаривай мне дела — я запишу и напомню.\n"
            "Например: «завтра в 10 позвонить в банк», «каждый вторник в 19 спортзал».\n\n"
            "Запоминаю важное о тебе: сам из разговоров или по просьбе — «запомни, что мама — Ольга, ДР 12 марта». "
            "Посмотреть — /memory, удалить — «удали факт 3».\n\n"
            "/tasks — список дел\n/lists — списки (покупки, в поездку…)\n"
            "/digest — сводка на сегодня\n/review — что переносим?\n"
            "/memory — что я о тебе знаю\n/habits — твои привычки по времени\n"
            "/reset — забыть контекст диалога\n\n"
            f"Часовой пояс: {user.timezone}\nУтренняя сводка: {digest} "
            "(«присылай сводку в 8:30» / «не присылай сводку»)\n"
            f"Вечерний разбор: {review_at} («разбор в 22:00» / «не нужен вечерний разбор»)"
        )

    @router.message(Command("digest"))
    async def digest(message: Message):
        user = await ensure_user(message)
        d = await build_digest(repo, user, utcnow())
        await message.answer(format_digest(d, user.timezone))

    @router.message(Command("review"))
    async def review(message: Message):
        user = await ensure_user(message)
        r = await build_review(repo, user, utcnow())
        if not r.should_send:
            await message.answer("На сегодня дел со сроком не было — разбирать нечего.")
            return
        for text, kb in review_messages(r, user.timezone):
            await message.answer(text, reply_markup=kb)

    @router.callback_query(ReviewAction.filter(F.action == "all_tomorrow"))
    async def on_review_all(cb: CallbackQuery):
        user = await ensure_user(cb)
        moved = await postpone_all_to_tomorrow(repo, user, utcnow())
        text = (f"➡️ Перенёс на завтра: {len(moved)}" if moved else "Переносить уже нечего — всё разобрано 👍")
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{text}", reply_markup=None)
        await cb.answer()

    @router.callback_query(DateAction.filter())
    async def on_date_action(cb: CallbackQuery, callback_data: DateAction):
        user = await ensure_user(cb)
        memory = await repo.memory_by_id(user.id, callback_data.memory_id)
        if memory is None:
            await cb.answer("Этой даты в памяти уже нет")
            return
        if callback_data.action == "skip":
            await cb.message.edit_text(f"{cb.message.html_text}\n\n👌 Хорошо, без напоминаний", reply_markup=None)
            await cb.answer()
            return
        occurs = datetime.strptime(str(callback_data.occurs), "%Y%m%d").date()
        now = utcnow()
        gift = callback_data.action == "gift"
        due = reminder_at(occurs, GIFT_OFFSET if gift else CONGRATS_OFFSET, now, user.timezone)
        title = f"{'Купить подарок' if gift else 'Поздравить'} — {memory.content}"
        task = await repo.create_task(user.id, title=title, due_at=due)
        done = callback_data.done | (DATE_GIFT if gift else DATE_CONGRATS)
        note = f"✅ Напомню {'купить подарок' if gift else 'поздравить'}: {format_local(task.due_at, user.timezone)}"
        today = to_local(now, user.timezone).date()
        up = UpcomingDate(memory=memory, occurs=occurs, days=(occurs - today).days)
        await cb.message.edit_text(f"{cb.message.html_text}\n{note}", reply_markup=date_alert_kb(up, done))
        await cb.answer()

    @router.message(Command("habits"))
    async def habits(message: Message):
        user = await ensure_user(message)
        await message.answer(habits_text(await load_profile(repo, user)))

    @router.message(Command("lists"))
    async def lists(message: Message):
        user = await ensure_user(message)
        text, kb = lists_menu(await repo.user_lists(user.id))
        await message.answer(text, reply_markup=kb)

    async def show_list_card(message: Message, user, list_id: int):
        lst = await repo.get_list(user.id, list_id)
        if lst:
            text, kb = list_card(lst, await repo.list_items(lst.id))
            await message.answer(text, reply_markup=kb)

    @router.callback_query(ListAction.filter())
    async def on_list_action(cb: CallbackQuery, callback_data: ListAction):
        user = await ensure_user(cb)
        if callback_data.action == "open":
            await show_list_card(cb.message, user, callback_data.list_id)
            await cb.answer()
            return
        if callback_data.action == "toggle":
            lst = await repo.toggle_item(user.id, callback_data.item_id)
        else:  # clear
            lst = await repo.get_list(user.id, callback_data.list_id)
            if lst:
                await repo.clear_done(lst.id)
        if lst is None:
            await cb.answer("Этого списка или пункта уже нет")
            return
        text, kb = list_card(lst, await repo.list_items(lst.id))
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

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

    @router.message(F.photo | F.document.mime_type.startswith("image/"))
    async def on_photo(message: Message):
        # регистрируется раньше on_text: у фото с подписью есть caption, но картинку терять нельзя
        media = message.photo[-1] if message.photo else message.document  # photo[-1] — самый крупный размер
        if media.file_size and media.file_size > MAX_IMAGE_BYTES:
            await message.answer("Картинка слишком большая — пришли её как фото, а не файлом.")
            return
        user = await ensure_user(message)
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            data = (await message.bot.download(media)).getvalue()
            mime = "image/jpeg" if message.photo else (message.document.mime_type or "image/jpeg")
            images = [Image(data=data, mime=mime)]
            caption = message.caption or ""
            try:
                if origin := message.forward_origin:
                    reply = await agent.handle_forwarded(user, caption, forward_sender(origin, message),
                                                         origin.date, images=images)
                else:
                    reply = await agent.handle_photo(user, images, caption)
            except Exception:
                log.exception("photo processing failed")
                await message.answer("Не получилось разобрать фото. Возможно, текущая модель не умеет "
                                     "работать с картинками — напиши, что на нём, текстом.")
                return
            await send_reply(message, user, reply)

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
        await message.answer("В пересланном сообщении я понимаю текст, фото и голосовые — здесь их нет. "
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
        await send_reply(message, user, reply)

    async def send_reply(message: Message, user, reply):
        await message.answer(html.escape(reply.text))
        # дедуп: одна задача могла быть затронута несколько раз за ход
        seen = {}
        for t in reply.touched:
            seen[t.id] = t
        for t in seen.values():
            if t.status == "open":
                await message.answer(task_card(t, user.timezone, auto_time=t.id in reply.auto_time),
                                     reply_markup=created_kb(t))
        for list_id in reply.lists_touched:
            await show_list_card(message, user, list_id)

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
        elif callback_data.action == "tomorrow":
            task = await postpone_to_tomorrow(repo, user, tid, utcnow())
            note = f"➡️ Перенёс на {format_local(task.due_at, user.timezone)}" if task else ""
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
