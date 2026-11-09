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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


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
