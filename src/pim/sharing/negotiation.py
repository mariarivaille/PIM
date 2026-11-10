"""Переговоры по поручению: стороны по очереди принимают, отклоняют или корректируют.

Ход всегда у того, кто получил последнюю версию: pending — у исполнителя, countered — у поручившего.
Корректировка — новая версия (assign_round + 1) и передача хода. Договорились — когда сторона, получившая
версию, её принимает: автор версии согласен с ней по определению, вместе это два принятия подряд.
"""

from dataclasses import dataclass

from pim.core.models import Task, TaskStatus
from pim.core.repository import Repository

PENDING, COUNTERED, ACCEPTED, DECLINED = "pending", "countered", "accepted", "declined"


@dataclass
class Turn:
    task: Task | None
    error: str | None = None


def turn_of(task: Task) -> int | None:
    """Чей сейчас ход: id исполнителя, поручившего или None, если переговоры закончены."""
    if task.assign_state in (None, PENDING):
        return task.user_id
    if task.assign_state == COUNTERED:
        return task.assigned_by
    return None


def other_party(task: Task, user_id: int) -> int:
    return task.assigned_by if user_id == task.user_id else task.user_id


async def check_turn(repo: Repository, user_id: int, task_id: int, round_: int | None = None) -> Turn:
    task = await repo.get_party_task(user_id, task_id)
    if task is None or task.status != TaskStatus.OPEN:
        return Turn(None, "Этого поручения уже нет")
    if turn_of(task) is None:
        return Turn(None, "По этому поручению уже договорились")
    if round_ is not None and round_ != task.assign_round:
        return Turn(None, "Это уже неактуальная версия — смотри последнее сообщение")
    if turn_of(task) != user_id:
        return Turn(None, "Сейчас ждём ответа другой стороны")
    return Turn(task)


async def respond(repo: Repository, user_id: int, task_id: int, round_: int, accept: bool) -> Turn:
    turn = await check_turn(repo, user_id, task_id, round_)
    if turn.task is None:
        return turn
    return Turn(await repo.set_negotiation(task_id, ACCEPTED if accept else DECLINED))


async def counter(repo: Repository, user_id: int, task_id: int) -> Task | None:
    """После корректировки: новая версия, ход переходит другой стороне."""
    task = await repo.get_party_task(user_id, task_id)
    if task is None:
        return None
    state = COUNTERED if user_id == task.user_id else PENDING
    return await repo.set_negotiation(task_id, state, bump_round=True)
