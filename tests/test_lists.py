from types import SimpleNamespace

import pytest

from pim.agent.tools import ToolContext, run_tool
from pim.lists.matching import display_name, match_items, match_list, normalize
from pim.telegram.keyboards import list_card, lists_menu

from tests.conftest import TZ


def named(*names):
    return [SimpleNamespace(name=n) for n in names]


@pytest.mark.parametrize("asked, expected", [
    ("покупки", "Покупки"),
    ("список покупок", "Покупки"),
    ("ПОКУПОК", "Покупки"),
    ("в поездку", "В поездку"),
    ("фильмы", None),
])
def test_match_list(asked, expected):
    found = match_list(named("Покупки", "В поездку"), asked)
    assert (found.name if found else None) == expected


def test_match_list_ambiguous_stem_is_not_guessed():
    assert match_list(named("Подарки маме", "Подарки папе"), "подарки") is None


def test_normalize_and_display():
    assert normalize("Список  покупок!") == "покупок"
    assert display_name("список покупок") == "Покупок"
    assert display_name("покупки") == "Покупки"


def test_match_items():
    items = [SimpleNamespace(text=t) for t in ("молоко 2,5%", "хлеб", "яйца")]
    found, missing = match_items(items, ["Хлеб", "молоко", "сыр"])
    assert [i.text for i in found] == ["хлеб", "молоко 2,5%"] and missing == ["сыр"]


async def test_add_check_remove_show(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "add_to_list", {"list": "покупки", "items": ["молоко", "хлеб", "Молоко"]})
    assert res["created_list"] and res["list"] == "Покупки"
    assert res["added"] == ["молоко", "хлеб"] and res["already_there"] == ["Молоко"]

    # строка вместо массива, другое написание названия списка
    res = await run_tool(ctx, "add_to_list", {"list": "список покупок", "items": "яйца, сыр и хлеб,кефир 2,5%"})
    assert not res["created_list"] and res["already_there"] == ["хлеб"]
    assert res["added"] == ["яйца", "сыр", "кефир 2,5%"]

    res = await run_tool(ctx, "check_off", {"list": "покупки", "items": ["молоко", "икра"]})
    assert res["checked"] == ["молоко"] and res["not_found"] == ["икра"]
    res = await run_tool(ctx, "remove_from_list", {"list": "покупки", "items": "сыр, кефир"})
    assert res["removed"] == ["сыр", "кефир 2,5%"]

    res = await run_tool(ctx, "show_list", {"list": "Покупки"})
    assert [(i["text"], i["done"]) for i in res["items"]] == [("хлеб", False), ("яйца", False), ("молоко", True)]
    assert len(ctx.lists_touched) == 1  # одна карточка списка на ход, а не по карточке на вызов


async def test_unknown_list_and_delete(repo, user):
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "add_to_list", {"list": "в поездку", "items": ["паспорт"]})
    err = await run_tool(ctx, "show_list", {"list": "фильмы"})
    assert "«В поездку»" in err["error"]
    assert (await run_tool(ctx, "delete_list", {"list": "в поездку"}))["deleted_list"] == "В поездку"
    assert await repo.user_lists(user.id) == [] and ctx.lists_touched == []


async def test_lists_are_isolated(repo, user):
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "add_to_list", {"list": "покупки", "items": ["молоко"]})
    [(lst, _, _)] = await repo.user_lists(user.id)
    [item] = await repo.list_items(lst.id)

    other = await repo.get_or_create_user(2, 200, TZ)
    assert await repo.get_list(other.id, lst.id) is None
    assert await repo.toggle_item(other.id, item.id) is None
    assert "error" in await run_tool(ToolContext(repo, other), "show_list", {"list": "покупки"})
    assert await repo.delete_list(other.id, lst.id) is None


async def test_toggle_and_clear(repo, user):
    lst = await repo.create_list(user.id, "Покупки")
    await repo.add_items(lst.id, ["молоко", "хлеб"])
    milk, _ = await repo.list_items(lst.id)
    assert (await repo.toggle_item(user.id, milk.id)).id == lst.id
    assert [i.done for i in await repo.list_items(lst.id)] == [False, True]  # отмеченные — в конце
    assert await repo.clear_done(lst.id) == 1
    assert [i.text for i in await repo.list_items(lst.id)] == ["хлеб"]


async def test_list_card_and_menu(repo, user):
    lst = await repo.create_list(user.id, "Покупки")
    text, kb = list_card(lst, [])
    assert text.startswith("🛒 <b>Покупки</b>") and "Пусто" in text

    await repo.add_items(lst.id, ["молоко", "хлеб"])
    items = await repo.list_items(lst.id)
    await repo.toggle_item(user.id, items[1].id)
    text, kb = list_card(lst, await repo.list_items(lst.id))
    assert "отмечено 1 из 2" in text
    assert [row[0].text for row in kb.inline_keyboard] == ["☐ молоко", "✅ хлеб", "🧹 Убрать отмеченные"]

    text, kb = lists_menu(await repo.user_lists(user.id))
    assert kb.inline_keyboard[0][0].text == "🛒 Покупки (1)"
    assert lists_menu([])[1] is None
