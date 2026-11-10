import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from pim.agent.agent import Agent
from pim.config import Settings
from pim.core.models import TaskStatus, utcnow
from pim.core.repository import Repository
from pim.core.timeutils import format_local, to_local
from pim.learning.timing import load_profile
from pim.llm.base import Image
from pim.reminders.digest import build_digest
from pim.planning.day import apply_plan
from pim.reminders.dates import CONGRATS_OFFSET, GIFT_OFFSET, UpcomingDate, reminder_at
from pim.reminders.review import build_review, postpone_all_to_tomorrow, postpone_to_tomorrow
from pim.stt.base import Transcriber
from pim.telegram.bot_helpers import INVITE_PREFIX, invite_code
from pim.telegram.keyboards import (
    AssignAction,
    DateAction,
    ListAction,
    PlanAction,
    ReviewAction,
    TaskAction,
    DATE_CONGRATS,
    DATE_GIFT,
    adjusted_text,
    assign_reply_text,
    created_kb,
    date_alert_kb,
    format_digest,
    habits_text,
    list_card,
    lists_menu,
    memory_list,
    people_text,
    plan_make_kb,
    plan_message,
    review_messages,
    task_card,
)

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
# С этого часа /plan планирует уже завтрашний день
PLAN_TOMORROW_FROM = 20


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
        return await repo.get_or_create_user(tg_user.id, chat.id, settings.default_timezone,
                                             name=tg_user.full_name)

    @router.message(CommandStart())
    async def start(message: Message):
        user = await ensure_user(message)
        if code := invite_code(message.text or ""):
            inviter_id = await repo.accept_invite(code, user.id)
            if inviter_id is None:
                await message.answer("Это приглашение уже использовано или устарело — попроси новое.")
            else:
                inviter = await repo.get_user(inviter_id)
                await message.answer(f"🤝 Вы с {html.escape(inviter.name or 'пригласившим')} теперь связаны в Pim: "
                                     "можно поручать друг другу дела и вести общие списки.")
                await message.bot.send_message(inviter.chat_id, f"🤝 {html.escape(user.name or 'Новый участник')} "
                                               "присоединяется к Pim по твоему приглашению.")
        digest = f"в {user.digest_time}" if user.digest_time else "выключена"
        review_at = f"в {user.review_time}" if user.review_time else "выключен"
        await message.answer(
            "Привет! Я Pim. Пиши или наговаривай мне дела — я запишу и напомню.\n"
            "Например: «завтра в 10 позвонить в банк», «каждый вторник в 19 спортзал».\n\n"
            "Запоминаю важное о тебе: сам из разговоров или по просьбе — «запомни, что мама — Ольга, ДР 12 марта». "
            "Посмотреть — /memory, удалить — «удали факт 3».\n\n"
            "/tasks — список дел\n/lists — списки (покупки, в поездку…)\n"
            "/digest — сводка на сегодня\n/plan — спланировать день\n/review — что переносим?\n"
            "/memory — что я о тебе знаю\n/habits — твои привычки по времени\n"
            "/people — близкие: поручения и общие списки\n/invite — пригласить близкого\n"
            "/reset — забыть контекст диалога\n\n"
            f"Часовой пояс: {user.timezone}\nУтренняя сводка: {digest} "
            "(«присылай сводку в 8:30» / «не присылай сводку»)\n"
            f"Вечерний разбор: {review_at} («разбор в 22:00» / «не нужен вечерний разбор»)"
        )

    @router.message(Command("digest"))
    async def digest(message: Message):
        user = await ensure_user(message)
        d = await build_digest(repo, user, utcnow())
        kb = plan_make_kb(d.day.date()) if d.undated or d.overdue else None
        await message.answer(format_digest(d, user.timezone), reply_markup=kb)

    async def make_plan(message: Message, user, day):
        if agent.planner is None:
            await message.answer("Планирование сейчас недоступно.")
            return
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            try:
                plan = await agent.planner.plan(user, day)
            except Exception:
                log.exception("planning failed")
                await message.answer("Не получилось составить план, попробуй ещё раз.")
                return
        text, kb = plan_message(plan, user.timezone)
        await message.answer(text, reply_markup=kb)

    @router.message(Command("plan"))
    async def plan(message: Message):
        user = await ensure_user(message)
        local = to_local(utcnow(), user.timezone)
        # вечером планируем уже завтра
        day = local.date() + timedelta(days=1) if local.hour >= PLAN_TOMORROW_FROM else local.date()
        await make_plan(message, user, day)

    @router.callback_query(PlanAction.filter())
    async def on_plan_action(cb: CallbackQuery, callback_data: PlanAction):
        user = await ensure_user(cb)
        if callback_data.action == "make":
            await cb.answer()
            await make_plan(cb.message, user, datetime.strptime(str(callback_data.day), "%Y%m%d").date())
            return
        if callback_data.action == "cancel":
            await cb.message.edit_text(f"{cb.message.html_text}\n\n✖️ Не применяю — дела остались как были",
                                       reply_markup=None)
            await cb.answer()
            return
        moved = await apply_plan(repo, user, callback_data.plan_id)
        if moved is None:
            await cb.answer("Этого плана уже нет")
            return
        note = f"✅ Готово: поставил время и напоминания для {moved} дел" if moved else "План уже применён"
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{note}", reply_markup=None)
        await cb.answer()

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

    @router.message(Command("invite"))
    async def invite(message: Message):
        user = await ensure_user(message)
        code = await repo.create_invite(user.id)
        me = await message.bot.me()
        link = f"https://t.me/{me.username}?start={INVITE_PREFIX}{code}"
        await message.answer(
            "Отправь эту ссылку близкому — откроет, нажмёт «Старт», и вы будете связаны в Pim:\n\n"
            f"{link}\n\nСсылка одноразовая и действует 7 дней.", disable_web_page_preview=True)

    @router.message(Command("people"))
    async def people(message: Message):
        user = await ensure_user(message)
        await message.answer(people_text(await repo.connections(user.id)))

    @router.message(Command("lists"))
    async def lists(message: Message):
        user = await ensure_user(message)
        text, kb = lists_menu(await repo.user_lists(user.id), await repo.shared_list_ids(user.id))
        await message.answer(text, reply_markup=kb)

    async def show_list_card(message: Message, user, list_id: int):
        lst = await repo.get_list(user.id, list_id)
        if lst:
            text, kb = list_card(lst, await repo.list_items(lst.id), lst.id in await repo.shared_list_ids(user.id))
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
        text, kb = list_card(lst, await repo.list_items(lst.id), lst.id in await repo.shared_list_ids(user.id))
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
        if user.adjusting_task_id and not message.forward_origin:
            await adjust_assignment(message, user, text)
            return
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

    async def adjust_assignment(message: Message, user, text: str):
        task_id = user.adjusting_task_id
        await repo.set_adjusting(user.id, None)
        before = await repo.get_task(user.id, task_id)
        if before is None or before.status != TaskStatus.OPEN:
            await process(message, await repo.get_user(user.id), text)  # поручения уже нет — обычное сообщение
            return
        before_title = before.title
        before_due = format_local(before.due_at, user.timezone) if before.due_at else "без срока"
        try:
            reply = await agent.handle_adjustment(user, before, text)
        except Exception:
            log.exception("adjustment failed")
            await message.answer("Не получилось поправить, попробуй ещё раз: нажми «✏️ Скорректировать».")
            return
        await send_reply(message, user, reply)
        after = await repo.get_task(user.id, task_id)
        after_due = format_local(after.due_at, user.timezone) if after.due_at else "без срока"
        if after.title == before_title and after_due == before_due:
            return
        await repo.set_assign_state(user.id, task_id, "accepted")
        by = await repo.get_user(after.assigned_by)
        name = await repo.display_name(by.id, user.id)
        await message.bot.send_message(by.chat_id, adjusted_text(name, before_title, before_due, after, by.timezone))

    @router.callback_query(AssignAction.filter())
    async def on_assign_action(cb: CallbackQuery, callback_data: AssignAction):
        user = await ensure_user(cb)
        task = await repo.get_task(user.id, callback_data.task_id)
        if task is None or task.assigned_by is None or task.status != TaskStatus.OPEN:
            await cb.answer("Этого поручения уже нет")
            return
        if callback_data.action == "adjust":
            await repo.set_adjusting(user.id, task.id)
            await cb.message.answer("✏️ Напиши, что поправить: например «давай в четверг в 13», «лучше в 14:00» "
                                    "или «не купить, а заказать».")
            await cb.answer()
            return
        state = "accepted" if callback_data.action == "accept" else "declined"
        task = await repo.set_assign_state(user.id, task.id, state)
        by = await repo.get_user(task.assigned_by)
        name = await repo.display_name(by.id, user.id)
        await cb.message.bot.send_message(by.chat_id, assign_reply_text(task, name, callback_data.action))
        if state == "accepted":
            await cb.message.edit_text(f"{cb.message.html_text}\n\n👍 Принято", reply_markup=created_kb(task))
        else:
            await cb.message.edit_text(f"{cb.message.html_text}\n\n✖️ Отклонено", reply_markup=None)
        await cb.answer()

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
        if reply.plan is not None:
            text, kb = plan_message(reply.plan, user.timezone)
            await message.answer(text, reply_markup=kb)

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
