from datetime import timedelta
from types import SimpleNamespace

from aiogram.types import Chat, MessageOriginChannel, MessageOriginHiddenUser, MessageOriginUser
from aiogram.types import User as TgUser

from pim.agent.agent import Agent, History
from pim.agent.tools import ToolContext, run_tool
from pim.core.models import utcnow
from pim.core.timeutils import parse_local, to_local
from pim.llm.base import Message, ToolCall
from pim.telegram.handlers import forward_sender

from tests.conftest import TZ
from tests.test_agent import ScriptedLLM


async def test_relative_dates_count_from_sending_time(repo, user):
    friday = parse_local("2026-11-06T18:40", TZ)  # пятница
    ctx = ToolContext(repo, user, reference_time=friday)
    res = await run_tool(ctx, "create_task", {"title": "созвон", "day": "tomorrow", "time": "15:00"})
    assert to_local(ctx.touched[0].due_at, TZ).strftime("%a %d.%m %H:%M") == "Sat 07.11 15:00"
    assert res["created"]["already_passed"]  # суббота 07.11 уже прошла

    await run_tool(ctx, "create_task", {"title": "x", "in_minutes": 60})
    assert to_local(ctx.touched[1].due_at, TZ).strftime("%d.%m %H:%M") == "06.11 19:40"


async def test_future_deadline_from_forward_is_not_flagged(repo, user):
    ctx = ToolContext(repo, user, reference_time=utcnow() - timedelta(hours=1))
    res = await run_tool(ctx, "create_task", {"title": "x", "day": "tomorrow", "time": "15:00"})
    assert "already_passed" not in res["created"]


async def test_handle_forwarded_wraps_text_and_uses_sent_time(repo, user):
    sent = utcnow() - timedelta(days=1)
    llm = ScriptedLLM([
        Message("assistant", tool_calls=[ToolCall("c1", "create_task",
                                                  {"title": "Созвон с Андреем", "day": "tomorrow", "time": "15:00"})]),
        Message("assistant", "Записал созвон с Андреем на сегодня 15:00."),
    ])
    agent = Agent(llm, repo, History(repo, 10))
    reply = await agent.handle_forwarded(user, "давай созвонимся завтра в 15", "Андрей", sent)

    user_msg = llm.calls[0][-1].content
    assert user_msg.startswith("[Пересланное сообщение от Андрей, отправлено ")
    assert user_msg.endswith("давай созвонимся завтра в 15")
    # «завтра» от вчерашнего сообщения — это сегодня
    assert to_local(reply.touched[0].due_at, TZ).date() == to_local(utcnow(), TZ).date()


async def test_remember_in_forwarded_text_is_not_users_request(repo, user):
    user.auto_memory = False
    ctx = ToolContext(repo, user, user_text="[Пересланное сообщение от Андрей, отправлено пн]\nзапомни мой номер")
    assert "error" in await run_tool(ctx, "remember", {"content": "номер Андрея"})
    user.auto_memory = True


def test_forward_sender():
    me = TgUser(id=1, is_bot=False, first_name="Мария")
    msg = SimpleNamespace(from_user=me)
    now = utcnow()
    andrey = TgUser(id=2, is_bot=False, first_name="Андрей", last_name="Петров")
    assert forward_sender(MessageOriginUser(type="user", date=now, sender_user=andrey), msg) == "Андрей Петров"
    assert forward_sender(MessageOriginUser(type="user", date=now, sender_user=me), msg) == "тебя самого"
    assert forward_sender(MessageOriginHiddenUser(type="hidden_user", date=now, sender_user_name="Ольга"), msg) == "Ольга"
    channel = Chat(id=-100, type="channel", title="Школа №5")
    origin = MessageOriginChannel(type="channel", date=now, chat=channel, message_id=1, author_signature="Завуч")
    assert forward_sender(origin, msg) == "Завуч (Школа №5)"


async def test_past_deadline_is_not_reminded(repo, user):
    ctx = ToolContext(repo, user, reference_time=parse_local("2026-11-06T18:40", TZ))
    await run_tool(ctx, "create_task", {"title": "отчёт", "day": "today", "time": "18:00"})
    assert await repo.due_tasks(utcnow()) == []  # не звенит
    assert [t.title for t in await repo.list_open_tasks(user.id)] == ["отчёт"]  # но в списке есть


async def test_forwarded_remember_error_forbids_claiming(repo, user):
    ctx = ToolContext(repo, user, user_text="[Пересланное сообщение от Ольга, отправлено пн]\nзапомни номер")
    res = await run_tool(ctx, "remember", {"content": "номер Ольги"})
    assert "НЕ говори, что запомнил" in res["error"]


async def test_dates_from_users_comment_count_from_now(repo, user):
    week_ago = utcnow() - timedelta(days=7)
    ctx = ToolContext(repo, user, reference_time=week_ago)
    await run_tool(ctx, "create_task", {"title": "из поста", "day": "tomorrow", "time": "15:00"})
    await run_tool(ctx, "create_task", {"title": "из подписи", "day": "tomorrow", "time": "15:00",
                                        "date_from_forwarded": False})
    from_post, from_comment = ctx.touched
    assert to_local(from_post.due_at, TZ).date() == to_local(week_ago, TZ).date() + timedelta(days=1)
    assert to_local(from_comment.due_at, TZ).date() == to_local(utcnow(), TZ).date() + timedelta(days=1)


async def test_comment_day_detected_without_model_flag(repo, user):
    week_ago = utcnow() - timedelta(days=7)
    text = ("[Пересланное сообщение от Reddit, отправлено вт]\nНабор открыт до 22 ноября\n\n"
            "[Комментарий пользователя к пересланному] напомни в пятницу податься")
    ctx = ToolContext(repo, user, reference_time=week_ago, user_text=text)
    await run_tool(ctx, "create_task", {"title": "податься", "day": "fri", "time": "19:00"})
    due = to_local(ctx.touched[0].due_at, TZ)
    assert due.weekday() == 4 and due > to_local(utcnow(), TZ)  # ближайшая пятница, а не неделю назад


async def test_deadline_2359_is_not_a_night_reminder(repo, user):
    ctx = ToolContext(repo, user, asked_for_time=True)
    await run_tool(ctx, "create_task", {"title": "подать заявку", "due_at": "2030-11-22T23:59"})
    assert to_local(ctx.touched[0].due_at, TZ).hour == 10


async def test_reply_quote_goes_to_model_but_not_to_user_words(repo, user):
    from pim.agent.tools import own_words, with_reply
    text = with_reply(".", "твоё сообщение", "Запомни: позвонить маме в пятницу")
    assert own_words(text) == "."
    user.auto_memory = False
    ctx = ToolContext(repo, user, user_text=own_words(text))
    assert "error" in await run_tool(ctx, "remember", {"content": "позвонить маме"})  # «запомни» — не его слова
    user.auto_memory = True

    llm = ScriptedLLM([Message("assistant", "Что с этим сделать?")])
    agent = Agent(llm, repo, History(repo, 10))
    await agent.handle(user, text)
    assert "позвонить маме в пятницу" in llm.calls[0][-1].content


def test_replied_text():
    from pim.telegram.handlers import replied_text
    me = TgUser(id=1, is_bot=False, first_name="Мария")
    bot = SimpleNamespace(id=99)
    bot_msg = SimpleNamespace(from_user=TgUser(id=99, is_bot=True, first_name="Pim"), text="Купить хлеб", caption=None)
    msg = SimpleNamespace(text=".", caption=None, reply_to_message=bot_msg, quote=None, forward_origin=None,
                          bot=bot, from_user=me)
    assert replied_text(msg) == "[В ответ на твоё сообщение: «Купить хлеб»]\n."
    msg.quote = SimpleNamespace(text="хлеб")
    assert "«хлеб»" in replied_text(msg)
    plain = SimpleNamespace(text="привет", caption=None, reply_to_message=None, quote=None, forward_origin=None,
                            bot=bot, from_user=me)
    assert replied_text(plain) == "привет"
