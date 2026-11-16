import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from pim.core.models import utcnow
from pim.core.timeutils import parse_local
from pim.web.api import create_app
from pim.web.auth import InvalidInitData, sign_init_data, validate_init_data

from tests.conftest import TZ

TOKEN = "123456:TEST-token"


def init_data(user_id: int, age: int = 0, token: str = TOKEN) -> str:
    return sign_init_data({"auth_date": str(int(time.time()) - age), "query_id": "AAH",
                           "user": json.dumps({"id": user_id, "first_name": "Мария"}, ensure_ascii=False)}, token)


def test_validate_init_data():
    assert validate_init_data(init_data(7), TOKEN)["id"] == 7
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data(7, token="999:other"), TOKEN)  # подписано чужим ботом
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data(7, age=3 * 24 * 3600), TOKEN)  # старая подпись
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data(7).replace("%D0%9C", "%D0%9D"), TOKEN)  # подменили данные
    with pytest.raises(InvalidInitData):
        validate_init_data("user=%7B%7D", TOKEN)


@pytest.fixture
def client(repo):
    changed = []

    async def on_changed(task_id):
        changed.append(task_id)

    c = TestClient(create_app(repo, TOKEN, on_task_changed=on_changed))
    c.changed = changed
    return c


def auth(user_id: int) -> dict:
    return {"Authorization": "tma " + init_data(user_id)}


async def test_requires_telegram_and_known_user(client, user):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "tma garbage"}).status_code == 401
    assert client.get("/api/state", headers=auth(999)).status_code == 403  # не писал боту
    assert client.get("/").status_code == 200  # сама страница открыта, данные — только с подписью


async def test_state_tasks_calendar(client, repo, user):
    now = utcnow()
    await repo.create_task(user.id, "Банк", due_at=now + timedelta(hours=2), notes="до 18:00")
    await repo.create_task(user.id, "Старое", due_at=now - timedelta(days=2))
    await repo.create_task(user.id, "Когда-нибудь")
    start = now + timedelta(days=3)
    await repo.create_event(user.id, title="Маникюр", starts_at=start, ends_at=start + timedelta(minutes=90),
                            remind_before=45)
    vac = parse_local((now + timedelta(days=10)).astimezone().strftime("%Y-%m-%dT00:00"), TZ)
    await repo.create_event(user.id, title="Отпуск", starts_at=vac, ends_at=vac + timedelta(days=5), all_day=True)
    await repo.create_event(user.id, title="Спортзал", starts_at=now + timedelta(days=1),
                            ends_at=now + timedelta(days=1, hours=2), rrule="FREQ=WEEKLY")

    data = client.get("/api/state", headers=auth(user.id)).json()
    groups = {t["title"]: t["group"] for t in data["tasks"]}
    assert groups["Старое"] == "overdue" and groups["Когда-нибудь"] == "nodate"
    assert groups["Банк"] in ("today", "tomorrow")
    assert [p["title"] for p in data["periods"]] == ["Отпуск"] and data["periods"][0]["days"] == 5
    assert data["recurring"][0]["rule"].startswith("по ")
    items = [i["title"] for d in data["days"] for i in d["items"]]
    assert "Маникюр" in items and "Отпуск" not in items  # период — отдельным блоком, а не в дне
    assert len(data["strip"]) == 14 and data["strip"][0]["date"] == data["user"]["today"]


async def test_actions(client, repo, user):
    task = await repo.create_task(user.id, "Банк", due_at=utcnow() + timedelta(hours=2))
    other_task = await repo.create_task(user.id, "Почта", due_at=utcnow() + timedelta(hours=3))
    assert client.post(f"/api/tasks/{task.id}/done", headers=auth(user.id)).json()["ok"]
    assert client.post(f"/api/tasks/{task.id}/done", headers=auth(user.id)).status_code == 404  # уже закрыто
    assert client.post(f"/api/tasks/{other_task.id}/cancel", headers=auth(user.id)).json()["ok"]
    assert client.changed == [task.id, other_task.id]  # карточки в чате уберутся
    assert await repo.list_open_tasks(user.id) == []

    event = await repo.create_event(user.id, title="Маникюр", starts_at=utcnow() + timedelta(days=1),
                                    ends_at=utcnow() + timedelta(days=1, hours=1))
    assert client.delete(f"/api/events/{event.id}", headers=auth(user.id)).json()["ok"]
    assert await repo.list_events(user.id) == []

    mem = await repo.add_memory(user.id, "Кот — Барсик")
    data = client.get("/api/memory", headers=auth(user.id)).json()
    assert any(m["content"] == "Кот — Барсик" for s in data["sections"] for m in s["items"])
    assert client.delete(f"/api/memory/{mem.number}", headers=auth(user.id)).json()["ok"]
    assert await repo.list_memories(user.id) == []


async def test_cannot_touch_other_users_data(client, repo, user):
    other = await repo.get_or_create_user(2, 200, TZ)
    task = await repo.create_task(other.id, "Чужое", due_at=utcnow() + timedelta(hours=1))
    event = await repo.create_event(other.id, title="Чужое", starts_at=utcnow() + timedelta(days=1),
                                    ends_at=utcnow() + timedelta(days=1, hours=1))
    await repo.add_memory(other.id, "Чужой факт")
    assert client.post(f"/api/tasks/{task.id}/done", headers=auth(user.id)).status_code == 404
    assert client.post(f"/api/tasks/{task.id}/cancel", headers=auth(user.id)).status_code == 404
    assert client.delete(f"/api/events/{event.id}", headers=auth(user.id)).status_code == 404
    assert client.delete("/api/memory/1", headers=auth(user.id)).status_code == 404
    data = client.get("/api/state", headers=auth(user.id)).json()
    assert data["tasks"] == [] and data["days"] == []
    assert len(await repo.list_open_tasks(other.id)) == 1
