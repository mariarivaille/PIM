import pytest

from pim.core.db import init_db, make_engine, make_sessionmaker
from pim.core.repository import Repository

TZ = "Europe/Moscow"


@pytest.fixture
async def repo(tmp_path):
    # файловая БД, как в проде: у :memory: одно общее соединение, и параллельные пользователи на нём ломаются
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    yield Repository(make_sessionmaker(engine))
    await engine.dispose()


@pytest.fixture
async def user(repo):
    return await repo.get_or_create_user(1, 100, TZ)
