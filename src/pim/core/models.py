from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, TypeDecorator
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
    timezone: Mapped[str] = mapped_column(String(64))
    # Локальное время утренней сводки "HH:MM"; None = сводка выключена
    digest_time: Mapped[str | None] = mapped_column(String(5), default="09:00", server_default="09:00")
    # Локальная дата (YYYY-MM-DD) последней отправленной сводки — чтобы не слать дважды
    last_digest_on: Mapped[str | None] = mapped_column(String(10))
    # Локальное время вечернего разбора «что переносим?» "HH:MM"; None = выключен
    review_time: Mapped[str | None] = mapped_column(String(5), default="21:00", server_default="21:00")
    last_review_on: Mapped[str | None] = mapped_column(String(10))
    # Реплики раньше этого момента не попадают в контекст диалога (/reset)
    context_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
