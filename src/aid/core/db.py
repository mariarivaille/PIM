from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from aid.core.models import Base


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    # Для MVP хватает create_all; когда схема начнёт меняться — перейти на alembic
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
