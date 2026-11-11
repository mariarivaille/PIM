import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from pim.core.models import (
    ChatMessage,
    TaskCard,
    DateAlert,
    DayPlan,
    Connection,
    Invite,
    ItemList,
    ListItem,
    ListMember,
    Memory,
    MemoryKind,
    MemorySource,
    Task,
    TaskEvent,
    TaskEventKind,
    TaskStatus,
    User,
    utcnow,
)
from pim.core.timeutils import next_occurrence
from pim.core.yearly import parse_yearly_date


INVITE_TTL = timedelta(days=7)


def _apply_yearly_date(memory: Memory) -> bool:
    """Ежегодная дата из текста факта — в поля date_*; True, если нашлась."""
    found = parse_yearly_date(memory.content)
    memory.date_month = found.month if found else None
    memory.date_day = found.day if found else None
    memory.date_year = found.year if found else None
    return found is not None


class Repository:
    """Единая точка доступа к данным. Каждый метод — своя короткая транзакция."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self._sm = sessionmaker

    # --- users ---

    async def get_or_create_user(self, user_id: int, chat_id: int, default_tz: str,
                                 name: str | None = None) -> User:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user is None:
                user = User(id=user_id, chat_id=chat_id, timezone=default_tz, name=name)
                s.add(user)
            else:
                if user.chat_id != chat_id:
                    user.chat_id = chat_id
                if name and user.name != name:
                    user.name = name
            return user

    async def get_user(self, user_id: int) -> User | None:
        async with self._sm() as s:
            return await s.get(User, user_id)

    async def set_timezone(self, user_id: int, tz: str) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.timezone = tz

    async def set_digest_time(self, user_id: int, value: str | None) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.digest_time = value

    async def set_review_time(self, user_id: int, value: str | None) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.review_time = value

    async def users_with_review(self) -> list[User]:
        async with self._sm() as s:
            return list(await s.scalars(select(User).where(User.review_time.is_not(None))))

    async def mark_review_sent(self, user_id: int, local_date: str) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.last_review_on = local_date

    async def count_completed(self, user_id: int, since: datetime, until: datetime) -> int:
        """Сколько дел пользователь закрыл (сделано) в интервале."""
        async with self._sm() as s:
            return await s.scalar(
                select(func.count()).select_from(Task).where(
                    Task.user_id == user_id, Task.status == TaskStatus.DONE,
                    Task.completed_at >= since, Task.completed_at < until,
                )
            )

    async def users_with_digest(self) -> list[User]:
        async with self._sm() as s:
            return list(await s.scalars(select(User).where(User.digest_time.is_not(None))))

    async def mark_digest_sent(self, user_id: int, local_date: str) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.last_digest_on = local_date

    async def set_auto_memory(self, user_id: int, enabled: bool) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.auto_memory = enabled

    # --- memories ---
    # Пользователь видит и называет факты по `number` — своей нумерации 1, 2, 3…; номера не переиспользуются,
    # чтобы «удали факт 5» не попало в другой факт после удаления соседних.

    async def add_memory(self, user_id: int, content: str, kind: MemoryKind = MemoryKind.FACT,
                         source: MemorySource = MemorySource.EXPLICIT,
                         expires_at: datetime | None = None) -> Memory:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            number = (user.memory_seq or 0) + 1
            user.memory_seq = number
            memory = Memory(user_id=user_id, number=number, content=content, kind=kind, source=source,
                            expires_at=expires_at)
            _apply_yearly_date(memory)
            s.add(memory)
        return memory

    async def _memory_by_number(self, s: AsyncSession, user_id: int, number: int) -> Memory | None:
        return await s.scalar(select(Memory).where(Memory.user_id == user_id, Memory.number == number))

    async def get_memory(self, user_id: int, number: int) -> Memory | None:
        async with self._sm() as s:
            return await self._memory_by_number(s, user_id, number)

    async def list_memories(self, user_id: int, now: datetime | None = None) -> list[Memory]:
        """Актуальные воспоминания пользователя (истёкшие не возвращаются)."""
        now = now or utcnow()
        async with self._sm() as s:
            res = await s.scalars(
                select(Memory)
                .where(Memory.user_id == user_id, or_(Memory.expires_at.is_(None), Memory.expires_at > now))
                .order_by(Memory.number)
            )
            return list(res)

    async def update_memory(self, user_id: int, number: int, **fields) -> Memory | None:
        async with self._sm() as s, s.begin():
            memory = await self._memory_by_number(s, user_id, number)
            if not memory:
                return None
            for k, v in fields.items():
                setattr(memory, k, v)
            if "content" in fields:
                _apply_yearly_date(memory)
            return memory

    async def memory_by_id(self, user_id: int, memory_id: int) -> Memory | None:
        async with self._sm() as s:
            memory = await s.get(Memory, memory_id)
            return memory if memory and memory.user_id == user_id else None

    async def backfill_memory_dates(self) -> int:
        """Проставить ежегодные даты фактам, сохранённым до появления этой функции."""
        async with self._sm() as s, s.begin():
            found = 0
            for memory in await s.scalars(select(Memory).where(Memory.date_month.is_(None))):
                if _apply_yearly_date(memory):
                    found += 1
            return found

    async def memories_with_dates(self, user_id: int) -> list[Memory]:
        async with self._sm() as s:
            return list(await s.scalars(
                select(Memory).where(Memory.user_id == user_id, Memory.date_month.is_not(None))
            ))

    async def users_for_dates(self) -> list[User]:
        """Пользователи, у которых в памяти есть ежегодные даты."""
        async with self._sm() as s:
            return list(await s.scalars(
                select(User).where(User.id.in_(select(Memory.user_id).where(Memory.date_month.is_not(None))))
            ))

    async def date_alert_sent(self, memory_id: int, occurrence: str, lead_days: int) -> bool:
        async with self._sm() as s:
            return await s.scalar(select(func.count()).select_from(DateAlert).where(
                DateAlert.memory_id == memory_id, DateAlert.occurrence == occurrence,
                DateAlert.lead_days == lead_days)) > 0

    async def record_date_alert(self, memory_id: int, occurrence: str, lead_days: int) -> None:
        async with self._sm() as s, s.begin():
            s.add(DateAlert(memory_id=memory_id, occurrence=occurrence, lead_days=lead_days))

    async def mark_dates_checked(self, user_id: int, local_date: str) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.last_dates_on = local_date

    async def delete_memory(self, user_id: int, number: int) -> Memory | None:
        async with self._sm() as s, s.begin():
            memory = await self._memory_by_number(s, user_id, number)
            if not memory:
                return None
            await s.execute(delete(DateAlert).where(DateAlert.memory_id == memory.id))
            await s.delete(memory)
            return memory

    # --- chat history ---

    async def add_messages(self, user_id: int, *items: tuple[str, str], at: datetime | None = None) -> None:
        async with self._sm() as s, s.begin():
            at = at or utcnow()
            s.add_all(ChatMessage(user_id=user_id, role=role, content=content, created_at=at)
                      for role, content in items)

    async def recent_messages(self, user_id: int, limit: int) -> list[ChatMessage]:
        """Последние реплики после /reset, в хронологическом порядке."""
        async with self._sm() as s:
            user = await s.get(User, user_id)
            query = select(ChatMessage).where(ChatMessage.user_id == user_id)
            if user and user.context_since:
                query = query.where(ChatMessage.created_at > user.context_since)
            res = await s.scalars(query.order_by(ChatMessage.id.desc()).limit(limit))
            return list(reversed(list(res)))

    # --- списки ---

    @staticmethod
    def _accessible(user_id: int):
        """Условие «список доступен пользователю»: он владелец или участник общего списка."""
        return or_(ItemList.user_id == user_id,
                   ItemList.id.in_(select(ListMember.list_id).where(ListMember.user_id == user_id)))

    async def user_lists(self, user_id: int) -> list[tuple[ItemList, int, int]]:
        """Свои и общие списки с числом открытых и всех пунктов; свои — первыми."""
        async with self._sm() as s:
            res = await s.execute(
                select(
                    ItemList,
                    func.count(ListItem.id).filter(ListItem.done.is_(False)),
                    func.count(ListItem.id),
                )
                .outerjoin(ListItem, ListItem.list_id == ItemList.id)
                .where(self._accessible(user_id))
                .group_by(ItemList.id)
                .order_by(ItemList.user_id != user_id, ItemList.id)
            )
            return [(lst, open_, total) for lst, open_, total in res.all()]

    async def get_list(self, user_id: int, list_id: int) -> ItemList | None:
        async with self._sm() as s:
            return await s.scalar(select(ItemList).where(ItemList.id == list_id, self._accessible(user_id)))

    async def list_participants(self, list_id: int) -> list[int]:
        """Владелец и участники списка."""
        async with self._sm() as s:
            lst = await s.get(ItemList, list_id)
            if lst is None:
                return []
            members = await s.scalars(select(ListMember.user_id).where(ListMember.list_id == list_id))
            return [lst.user_id, *members]

    async def shared_list_ids(self, user_id: int) -> set[int]:
        """Какие из доступных пользователю списков общие (есть кто-то кроме владельца)."""
        async with self._sm() as s:
            return set(await s.scalars(
                select(ListMember.list_id).join(ItemList, ItemList.id == ListMember.list_id)
                .where(self._accessible(user_id))
            ))

    async def add_list_member(self, list_id: int, user_id: int) -> bool:
        """False — уже участник. Уже лежащие пункты помечаются «сообщёнными», чтобы не прилетели новичку пачкой."""
        async with self._sm() as s, s.begin():
            exists = await s.scalar(select(func.count()).select_from(ListMember).where(
                ListMember.list_id == list_id, ListMember.user_id == user_id))
            if exists:
                return False
            s.add(ListMember(list_id=list_id, user_id=user_id))
            for item in await s.scalars(select(ListItem).where(ListItem.list_id == list_id)):
                item.notified = True
            return True

    async def create_list(self, user_id: int, name: str) -> ItemList:
        async with self._sm() as s, s.begin():
            lst = ItemList(user_id=user_id, name=name)
            s.add(lst)
        return lst

    async def delete_list(self, user_id: int, list_id: int) -> ItemList | None:
        """Удалить может только владелец (участник общего списка — нет)."""
        async with self._sm() as s, s.begin():
            lst = await s.get(ItemList, list_id)
            if not lst or lst.user_id != user_id:
                return None
            await s.execute(delete(ListItem).where(ListItem.list_id == list_id))
            await s.execute(delete(ListMember).where(ListMember.list_id == list_id))
            await s.delete(lst)
            return lst

    async def list_items(self, list_id: int) -> list[ListItem]:
        """Сначала неотмеченные, потом отмеченные — в порядке добавления."""
        async with self._sm() as s:
            res = await s.scalars(
                select(ListItem).where(ListItem.list_id == list_id).order_by(ListItem.done, ListItem.id)
            )
            return list(res)

    async def add_items(self, list_id: int, texts: list[str], added_by: int | None = None) -> list[ListItem]:
        async with self._sm() as s, s.begin():
            items = [ListItem(list_id=list_id, text=t, added_by=added_by) for t in texts]
            s.add_all(items)
        return items

    async def set_items_done(self, list_id: int, item_ids: list[int], done: bool) -> None:
        async with self._sm() as s, s.begin():
            for item in await s.scalars(select(ListItem).where(ListItem.list_id == list_id,
                                                               ListItem.id.in_(item_ids))):
                item.done = done
                item.done_at = utcnow() if done else None

    async def toggle_item(self, user_id: int, item_id: int) -> ItemList | None:
        """Отметить/снять отметку (кнопка в Telegram). Возвращает список, если пункт принадлежит пользователю."""
        async with self._sm() as s, s.begin():
            item = await s.get(ListItem, item_id)
            lst = await s.scalar(select(ItemList).where(ItemList.id == item.list_id, self._accessible(user_id))) \
                if item else None
            if not lst:
                return None
            item.done = not item.done
            item.done_at = utcnow() if item.done else None
            return lst

    async def remove_items(self, list_id: int, item_ids: list[int]) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(delete(ListItem).where(ListItem.list_id == list_id, ListItem.id.in_(item_ids)))

    async def clear_done(self, list_id: int) -> int:
        async with self._sm() as s, s.begin():
            res = await s.execute(delete(ListItem).where(ListItem.list_id == list_id, ListItem.done.is_(True)))
            return res.rowcount

    # --- близкие: приглашения, связи, поручения ---

    async def create_invite(self, user_id: int) -> str:
        code = secrets.token_urlsafe(9)
        async with self._sm() as s, s.begin():
            s.add(Invite(code=code, user_id=user_id))
        return code

    async def valid_invite(self, code: str, now: datetime | None = None) -> Invite | None:
        async with self._sm() as s:
            invite = await s.scalar(select(Invite).where(Invite.code == code))
            if invite is None or invite.used_by is not None:
                return None
            if (now or utcnow()) - invite.created_at > INVITE_TTL:
                return None
            return invite

    async def accept_invite(self, code: str, user_id: int) -> int | None:
        """Связать пригласившего и нового пользователя. Возвращает id пригласившего или None."""
        invite = await self.valid_invite(code)
        if invite is None or invite.user_id == user_id:
            return None
        async with self._sm() as s, s.begin():
            inv = await s.get(Invite, invite.id)
            inv.used_by, inv.used_at = user_id, utcnow()
            inviter, joined = await s.get(User, invite.user_id), await s.get(User, user_id)
            for a, b in ((inviter, joined), (joined, inviter)):
                exists = await s.scalar(select(func.count()).select_from(Connection).where(
                    Connection.user_id == a.id, Connection.peer_id == b.id))
                if not exists:
                    s.add(Connection(user_id=a.id, peer_id=b.id, alias=b.name or "без имени"))
        return invite.user_id

    async def connections(self, user_id: int) -> list[tuple[Connection, User]]:
        async with self._sm() as s:
            res = await s.execute(
                select(Connection, User).join(User, User.id == Connection.peer_id)
                .where(Connection.user_id == user_id).order_by(Connection.id)
            )
            return [(c, u) for c, u in res.all()]

    async def display_name(self, viewer_id: int, user_id: int) -> str:
        """Как viewer видит user: его alias для этого человека, иначе имя из Telegram."""
        async with self._sm() as s:
            alias = await s.scalar(select(Connection.alias).where(Connection.user_id == viewer_id,
                                                                  Connection.peer_id == user_id))
            if alias:
                return alias
            user = await s.get(User, user_id)
            return (user.name if user else None) or "Близкий"

    async def _connection(self, s: AsyncSession, user_id: int, peer_id: int) -> Connection | None:
        return await s.scalar(select(Connection).where(Connection.user_id == user_id, Connection.peer_id == peer_id))

    async def set_alias(self, user_id: int, peer_id: int, alias: str) -> None:
        """Основное имя, под которым user_id видит peer_id. Прежнее основное не теряется — уходит в дополнительные."""
        async with self._sm() as s, s.begin():
            conn = await self._connection(s, user_id, peer_id)
            if conn and conn.alias != alias:
                names = [n for n in conn.nickname_list if n.casefold() != alias.casefold()]
                conn.nicknames = json.dumps([conn.alias, *names], ensure_ascii=False)
                conn.alias = alias

    async def add_nickname(self, user_id: int, peer_id: int, name: str) -> bool:
        """Ещё одно имя близкого. False — такое уже есть."""
        async with self._sm() as s, s.begin():
            conn = await self._connection(s, user_id, peer_id)
            if conn is None:
                return False
            known = [conn.alias, *conn.nickname_list]
            if any(n.casefold() == name.casefold() for n in known):
                return False
            conn.nicknames = json.dumps([*conn.nickname_list, name], ensure_ascii=False)
            return True

    async def remove_nickname(self, user_id: int, peer_id: int, name: str) -> bool:
        async with self._sm() as s, s.begin():
            conn = await self._connection(s, user_id, peer_id)
            if conn is None:
                return False
            left = [n for n in conn.nickname_list if n.casefold() != name.casefold()]
            if len(left) == len(conn.nickname_list):
                return False
            conn.nicknames = json.dumps(left, ensure_ascii=False) if left else None
            return True

    async def create_assigned_task(self, assignee_id: int, assigned_by: int, title: str,
                                   due_at: datetime | None = None, notes: str | None = None) -> Task:
        async with self._sm() as s, s.begin():
            task = Task(user_id=assignee_id, assigned_by=assigned_by, title=title, due_at=due_at, notes=notes,
                        assign_state="pending")
            s.add(task)
        return task

    async def get_party_task(self, user_id: int, task_id: int) -> Task | None:
        """Поручение, если пользователь в нём участвует — как исполнитель или как поручивший."""
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            if task and task.assigned_by is not None and user_id in (task.user_id, task.assigned_by):
                return task
            return None

    async def set_negotiation(self, task_id: int, state: str, bump_round: bool = False) -> Task | None:
        """Новое состояние переговоров. declined закрывает дело — сторонам сообщают отдельно, не «отменено»."""
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if task is None:
                return None
            task.assign_state = state
            if bump_round:
                task.assign_round = (task.assign_round or 1) + 1
            if state == "declined":
                task.status = TaskStatus.CANCELLED
                task.outcome_notified_at = utcnow()
            return task

    async def set_adjusting(self, user_id: int, task_id: int | None) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.adjusting_task_id = task_id

    async def pending_assignments(self) -> list[tuple[Task, User, User]]:
        """Поручения, о которых исполнитель ещё не знает: (дело, исполнитель, поручивший)."""
        async with self._sm() as s:
            by = aliased(User)
            res = await s.execute(
                select(Task, User, by).join(User, User.id == Task.user_id).join(by, by.id == Task.assigned_by)
                .where(Task.assigned_by.is_not(None), Task.assign_notified_at.is_(None))
            )
            return [tuple(r) for r in res.all()]

    async def pending_outcomes(self) -> list[tuple[Task, User, User]]:
        """Поручения, которые исполнитель сделал или удалил, а поручивший ещё не знает."""
        async with self._sm() as s:
            by = aliased(User)
            res = await s.execute(
                select(Task, User, by).join(User, User.id == Task.user_id).join(by, by.id == Task.assigned_by)
                .where(Task.assigned_by.is_not(None), Task.outcome_notified_at.is_(None),
                       Task.status.in_([TaskStatus.DONE, TaskStatus.CANCELLED]))
            )
            return [tuple(r) for r in res.all()]

    async def mark_task_notified(self, task_id: int, field: str) -> None:
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if task:
                setattr(task, field, utcnow())

    async def pending_list_additions(self) -> list[tuple[ItemList, int, list[ListItem]]]:
        """Новые пункты общих списков, о которых остальные участники ещё не знают: (список, кто добавил, пункты)."""
        async with self._sm() as s:
            res = await s.execute(
                select(ListItem, ItemList).join(ItemList, ItemList.id == ListItem.list_id)
                .where(ListItem.notified.is_(False), ListItem.added_by.is_not(None),
                       ItemList.id.in_(select(ListMember.list_id)))
                .order_by(ListItem.id)
            )
            groups: dict[tuple[int, int], tuple[ItemList, list[ListItem]]] = {}
            for item, lst in res.all():
                groups.setdefault((lst.id, item.added_by), (lst, []))[1].append(item)
            return [(lst, added_by, items) for (_, added_by), (lst, items) in groups.items()]

    async def mark_items_notified(self, item_ids: list[int]) -> None:
        async with self._sm() as s, s.begin():
            for item in await s.scalars(select(ListItem).where(ListItem.id.in_(item_ids))):
                item.notified = True

    # --- план дня ---

    async def save_day_plan(self, user_id: int, day: str, items: str) -> DayPlan:
        async with self._sm() as s, s.begin():
            plan = DayPlan(user_id=user_id, day=day, items=items)
            s.add(plan)
        return plan

    async def get_day_plan(self, user_id: int, plan_id: int) -> DayPlan | None:
        async with self._sm() as s:
            plan = await s.get(DayPlan, plan_id)
            return plan if plan and plan.user_id == user_id else None

    async def mark_plan_applied(self, plan_id: int) -> None:
        async with self._sm() as s, s.begin():
            plan = await s.get(DayPlan, plan_id)
            if plan:
                plan.applied_at = utcnow()

    # --- рефлексия: какую переписку ещё не разбирали ---

    async def users_pending_reflection(self, idle_before: datetime) -> list[User]:
        """Пользователи с автопамятью, у которых есть неразобранные реплики и разговор затих до idle_before."""
        async with self._sm() as s:
            last = (
                select(ChatMessage.user_id, func.max(ChatMessage.created_at).label("last_at"))
                .group_by(ChatMessage.user_id)
                .subquery()
            )
            res = await s.execute(
                select(User, last.c.last_at)
                .join(last, last.c.user_id == User.id)
                .where(User.auto_memory.is_(True))
            )
            pending = []
            # func.max наследует тип колонки (UTCDateTime), поэтому last_at уже aware UTC
            for user, last_at in res.all():
                if last_at <= idle_before and (user.reflected_until is None or last_at > user.reflected_until):
                    pending.append(user)
            return pending

    async def messages_to_reflect(self, user_id: int, limit: int) -> list[ChatMessage]:
        """Неразобранные реплики (не больше limit последних), в хронологическом порядке."""
        async with self._sm() as s:
            user = await s.get(User, user_id)
            query = select(ChatMessage).where(ChatMessage.user_id == user_id)
            if user and user.reflected_until:
                query = query.where(ChatMessage.created_at > user.reflected_until)
            res = await s.scalars(query.order_by(ChatMessage.id.desc()).limit(limit))
            return list(reversed(list(res)))

    async def mark_reflected(self, user_id: int, until: datetime) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.reflected_until = until

    async def reset_context(self, user_id: int, at: datetime | None = None) -> None:
        async with self._sm() as s, s.begin():
            user = await s.get(User, user_id)
            if user:
                user.context_since = at or utcnow()

    # --- tasks ---

    async def create_task(
        self,
        user_id: int,
        title: str,
        due_at: datetime | None = None,
        rrule: str | None = None,
        notes: str | None = None,
    ) -> Task:
        async with self._sm() as s, s.begin():
            task = Task(user_id=user_id, title=title, due_at=due_at, rrule=rrule, notes=notes)
            s.add(task)
        return task

    async def get_task(self, user_id: int, task_id: int) -> Task | None:
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            return task if task and task.user_id == user_id else None

    async def list_open_tasks(self, user_id: int) -> list[Task]:
        async with self._sm() as s:
            res = await s.scalars(
                select(Task)
                .where(Task.user_id == user_id, Task.status == TaskStatus.OPEN)
                .order_by(Task.due_at.is_(None), Task.due_at, Task.id)
            )
            return list(res)

    async def update_task(self, user_id: int, task_id: int, **fields) -> Task | None:
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if not task or task.user_id != user_id:
                return None
            for k, v in fields.items():
                setattr(task, k, v)
            if "due_at" in fields:
                task.reminded_at = None
            return task

    async def complete_task(self, user_id: int, task_id: int, tz: str) -> Task | None:
        """Закрывает задачу. Повторяющуюся — переносит на следующее срабатывание."""
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if not task or task.user_id != user_id or task.status != TaskStatus.OPEN:
                return None
            now = utcnow()
            nxt = None
            if task.rrule and task.due_at:
                nxt = next_occurrence(task.rrule, task.due_at, tz, after=max(now, task.due_at))
            reaction = int((now - task.reminded_at).total_seconds() // 60) if task.reminded_at else None
            s.add(TaskEvent(user_id=user_id, task_id=task.id, kind=TaskEventKind.DONE, at=now, minutes=reaction))
            if nxt:
                task.due_at = nxt
                task.reminded_at = None
            else:
                task.status = TaskStatus.DONE
                task.completed_at = now
            return task

    async def cancel_task(self, user_id: int, task_id: int) -> Task | None:
        task = await self.update_task(user_id, task_id, status=TaskStatus.CANCELLED)
        if task:
            await self.log_event(user_id, TaskEventKind.CANCELLED, utcnow(), task_id=task_id)
        return task

    async def add_task_card(self, task_id: int, chat_id: int, message_id: int) -> None:
        async with self._sm() as s, s.begin():
            s.add(TaskCard(task_id=task_id, chat_id=chat_id, message_id=message_id))

    async def pop_task_cards(self, task_id: int) -> list[TaskCard]:
        """Забирает (и забывает) все сообщения с карточкой дела."""
        async with self._sm() as s, s.begin():
            cards = list(await s.scalars(select(TaskCard).where(TaskCard.task_id == task_id)))
            for c in cards:
                await s.delete(c)
            return cards

    async def snooze_task(self, user_id: int, task_id: int, until: datetime) -> Task | None:
        task = await self.update_task(user_id, task_id, due_at=until)
        if task:
            now = utcnow()
            await self.log_event(user_id, TaskEventKind.SNOOZED, now, task_id=task_id,
                                 minutes=max(0, int((until - now).total_seconds() // 60)))
        return task

    async def log_event(self, user_id: int, kind: TaskEventKind, at: datetime, task_id: int | None = None,
                        minutes: int | None = None) -> None:
        async with self._sm() as s, s.begin():
            s.add(TaskEvent(user_id=user_id, task_id=task_id, kind=kind, at=at, minutes=minutes))

    async def task_events(self, user_id: int, since: datetime) -> list[TaskEvent]:
        async with self._sm() as s:
            return list(await s.scalars(
                select(TaskEvent).where(TaskEvent.user_id == user_id, TaskEvent.at >= since).order_by(TaskEvent.at)
            ))

    async def due_tasks(self, now: datetime) -> list[tuple[Task, User]]:
        """Открытые задачи, по которым пора напомнить и ещё не напомнили."""
        async with self._sm() as s:
            res = await s.execute(
                select(Task, User)
                .join(User, User.id == Task.user_id)
                .where(
                    Task.status == TaskStatus.OPEN,
                    Task.due_at.is_not(None),
                    Task.due_at <= now,
                    Task.reminded_at.is_(None),
                )
                .order_by(Task.due_at)
            )
            return [(t, u) for t, u in res.all()]

    async def mark_reminded(self, task_id: int, at: datetime, sent: bool = True) -> None:
        """sent=False — напоминание не отправлялось (срок уже прошёл при создании), в журнал не пишем."""
        async with self._sm() as s, s.begin():
            task = await s.get(Task, task_id)
            if task:
                task.reminded_at = at
                if sent:
                    s.add(TaskEvent(user_id=task.user_id, task_id=task.id, kind=TaskEventKind.REMINDED, at=at))
