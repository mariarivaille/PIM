import logging

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateColumn

from pim.core.models import Base

log = logging.getLogger(__name__)


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            # WAL: чтение не блокируется записью; busy_timeout: параллельные записи (несколько пользователей,
            # планировщик) ждут друг друга, а не падают с «database is locked»
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn) -> None:
    """Дописывает в существующие таблицы колонки, появившиеся в моделях.

    Временная замена миграциям: умеет только добавлять колонки. Когда понадобится
    менять/удалять — переходить на alembic.
    """
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                ddl = CreateColumn(col).compile(dialect=conn.dialect)
                log.info("migrate: add column %s.%s", table.name, col.name)
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
