import json
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """Хранит naive UTC в БД, наружу отдаёт aware UTC (SQLite не хранит tz)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed, pass an aware one")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram user id
    chat_id: Mapped[int] = mapped_column(BigInteger)
    # Имя из Telegram — так пользователя видят близкие («Мария просит…»)
    name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    # Локальное время утренней сводки "HH:MM"; None = сводка выключена
    digest_time: Mapped[str | None] = mapped_column(String(5), default="09:00", server_default="09:00")
    # Локальная дата (YYYY-MM-DD) последней отправленной сводки — чтобы не слать дважды
    last_digest_on: Mapped[str | None] = mapped_column(String(10))
    # Локальное время вечернего разбора «что переносим?» "HH:MM"; None = выключен
    review_time: Mapped[str | None] = mapped_column(String(5), default="21:00", server_default="21:00")
    last_review_on: Mapped[str | None] = mapped_column(String(10))
    # Когда последний раз проверяли важные даты (локальная дата) — раз в день
    last_dates_on: Mapped[str | None] = mapped_column(String(10))
    # Реплики раньше этого момента не попадают в контекст диалога (/reset)
    context_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Поручение, которое пользователь сейчас корректирует (после «✏️ Скорректировать» ждём его следующее сообщение)
    adjusting_task_id: Mapped[int | None] = mapped_column()
    # Последний выданный номер факта в памяти (у каждого пользователя своя нумерация)
    memory_seq: Mapped[int] = mapped_column(default=0, server_default="0")
    # Бот сам запоминает важное из разговоров (рефлексия); пользователь может выключить
    auto_memory: Mapped[bool] = mapped_column(default=True, server_default="1")
    # До какого момента переписка уже разобрана рефлексией
    reflected_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MemoryKind(StrEnum):
    PERSON = "person"          # люди: «мама — Ольга, ДР 12 марта»
    PREFERENCE = "preference"  # предпочтения: «не беспокоить до 9 утра»
    ROUTINE = "routine"        # распорядок и привычки: «спортзал вт/чт вечером»
    FACT = "fact"              # прочее о пользователе: «работаю из дома по пятницам»


class MemorySource(StrEnum):
    EXPLICIT = "explicit"  # пользователь сам попросил запомнить
    INFERRED = "inferred"  # бот вывел из разговора (следующий шаг)


class Memory(Base):
    """Долговременная память о пользователе: всё это попадает в системный промпт."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Номер факта у этого пользователя: так он виден в /memory и так его удаляют («удали факт 3»)
    number: Mapped[int | None] = mapped_column(index=True)
    kind: Mapped[MemoryKind] = mapped_column(String(16), default=MemoryKind.FACT)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[MemorySource] = mapped_column(String(16), default=MemorySource.EXPLICIT)
    # Для временных фактов («на этой неделе в отпуске»); None = бессрочно
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Ежегодная дата из текста факта (ДР, годовщина…) — для заблаговременных напоминаний; см. core/yearly.py
    date_month: Mapped[int | None] = mapped_column()
    date_day: Mapped[int | None] = mapped_column()
    date_year: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ChatMessage(Base):
    """История диалога (только текст user/assistant)."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    # Когда напомнить; None = задача без напоминания
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    # RFC 5545 RRULE без DTSTART, например "FREQ=WEEKLY;BYDAY=TU,TH"
    rrule: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[TaskStatus] = mapped_column(String(16), default=TaskStatus.OPEN, index=True)
    reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Поручение от близкого: кто поручил; когда исполнителю сообщили; когда поручителю сообщили об итоге
    assigned_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    assign_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    outcome_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Переговоры по поручению (pim/sharing/negotiation.py): pending — ход исполнителя, countered — ход
    # поручившего, accepted | declined — итог; assign_round — номер версии, чтобы не нажать кнопку под старой
    assign_state: Mapped[str | None] = mapped_column(String(16))
    assign_round: Mapped[int] = mapped_column(default=1, server_default="1")


class ItemList(Base):
    """Список (покупки, в поездку, фильмы…): пункты без времени и напоминаний.

    Владелец — user_id; общий доступ для близких будет отдельной таблицей участников.
    """

    __tablename__ = "lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ListItem(Base):
    __tablename__ = "list_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("lists.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(200))
    done: Mapped[bool] = mapped_column(default=False, server_default="0")
    # кто добавил и сообщили ли об этом остальным участникам общего списка
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    notified: Mapped[bool] = mapped_column(default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class DateAlert(Base):
    """Отправленное предупреждение о дате: чтобы «через 7 дней ДР мамы» пришло ровно один раз."""

    __tablename__ = "date_alerts"
    __table_args__ = (UniqueConstraint("memory_id", "occurrence", "lead_days"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_id: Mapped[int] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), index=True)
    occurrence: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD наступления
    lead_days: Mapped[int] = mapped_column()
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TaskEventKind(StrEnum):
    REMINDED = "reminded"    # бот прислал напоминание
    DONE = "done"            # отметил сделанным (minutes — через сколько после напоминания, если было)
    SNOOZED = "snoozed"      # отложил (minutes — на сколько)
    CANCELLED = "cancelled"  # удалил
    PLANNED = "planned"      # сам назначил время (at — на когда) — его предпочтения по часам


class TaskEvent(Base):
    """Журнал поведения пользователя с делами — из него pim/learning/timing.py выводит привычки по времени."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[int | None] = mapped_column()
    kind: Mapped[TaskEventKind] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    minutes: Mapped[int | None] = mapped_column()


class DayPlan(Base):
    """Предложенный план дня — ждёт подтверждения кнопкой «Поставить напоминания»."""

    __tablename__ = "day_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD, локальная дата
    items: Mapped[str] = mapped_column(Text)  # JSON: [{"task_id", "at" (ISO UTC), "minutes"}]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Connection(Base):
    """Близкие: с кем пользователь связан приглашением — им можно поручать дела и давать доступ к спискам.

    Хранится в обе стороны: (A, B) и (B, A); alias — как A называет B.
    """

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("user_id", "peer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    peer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    alias: Mapped[str] = mapped_column(String(100))
    # Дополнительные имена (JSON-список): «милый», «Андрюша» — по ним тоже находим человека
    nicknames: Mapped[str | None] = mapped_column(Text)

    @property
    def nickname_list(self) -> list[str]:
        return json.loads(self.nicknames) if self.nicknames else []
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Invite(Base):
    """Одноразовое приглашение: t.me/<bot>?start=join_<code>."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    used_by: Mapped[int | None] = mapped_column(BigInteger)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ListMember(Base):
    """Участник общего списка (кроме владельца)."""

    __tablename__ = "list_members"
    __table_args__ = (UniqueConstraint("list_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("lists.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
