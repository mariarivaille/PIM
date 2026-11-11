from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from pim.agent.tools import ToolContext, run_tool
from pim.telegram.cards import remember_card, remove_cards


class FakeBot:
    def __init__(self, too_old: set[int] = frozenset()):
        self.deleted, self.unbuttoned, self._too_old = [], [], too_old

    async def delete_message(self, chat_id, message_id):
        if message_id in self._too_old:
            raise TelegramBadRequest(method=None, message="message can't be deleted")
        self.deleted.append(message_id)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.unbuttoned.append(message_id)


def msg(mid: int):
    return SimpleNamespace(chat=SimpleNamespace(id=100), message_id=mid)


async def test_other_copies_are_deleted(repo, user):
    task = await repo.create_task(user.id, "банк")
    for mid in (1, 2, 3):
        await remember_card(repo, task.id, msg(mid))
    bot = FakeBot(too_old={3})
    await remove_cards(bot, repo, task.id, keep_message_id=2)  # под сообщением 2 нажали «Сделано»
    assert bot.deleted == [1] and bot.unbuttoned == [3]  # старое не удалить — хотя бы без кнопок
    assert await repo.pop_task_cards(task.id) == []  # больше не отслеживаются


async def test_closing_by_text_reports_task(repo, user):
    task = await repo.create_task(user.id, "банк")
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "complete_task", {"task_id": task.id})
    other = await repo.create_task(user.id, "почта")
    await run_tool(ctx, "cancel_task", {"task_id": other.id})
    assert [t.id for t in ctx.touched] == [task.id, other.id]  # send_reply уберёт их карточки из чата
