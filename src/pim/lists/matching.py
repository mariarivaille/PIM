"""Сопоставление того, как пользователь называет список и пункты, с тем, что лежит в БД.

Модель передаёт названия как услышала («список покупок», «покупок», «Покупки») — все они про один список.
"""

import re
from typing import Protocol, Sequence, TypeVar

# Префикс основы слова: «покупки» / «покупок» / «покупками» сходятся на «покуп»
STEM = 5


class _Named(Protocol):
    name: str


class _Texted(Protocol):
    text: str


N = TypeVar("N", bound=_Named)
T = TypeVar("T", bound=_Texted)


def normalize(text: str) -> str:
    text = re.sub(r"[^\w\s]+", " ", text.casefold().replace("ё", "е"))
    text = re.sub(r"^\s*(список|списка|списке|списку)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def display_name(name: str) -> str:
    """Как показывать список: без слова «список», с заглавной буквы."""
    clean = normalize(name) or name.strip()
    return clean[:1].upper() + clean[1:]


def match_list(lists: Sequence[N], name: str) -> N | None:
    """Точное совпадение после нормализации, иначе — единственный список с той же основой слова."""
    target = normalize(name)
    if not target:
        return None
    for lst in lists:
        if normalize(lst.name) == target:
            return lst
    if len(target) >= STEM:
        same_stem = [lst for lst in lists if normalize(lst.name)[:STEM] == target[:STEM]]
        if len(same_stem) == 1:
            return same_stem[0]
    return None


def match_items(items: Sequence[T], texts: Sequence[str]) -> tuple[list[T], list[str]]:
    """Пункты по словам пользователя: точно, иначе по вхождению («молоко» → «молоко 2,5%»).

    Возвращает (найденные пункты, тексты, которым ничего не нашлось).
    """
    found: list[T] = []
    missing: list[str] = []
    for text in texts:
        target = normalize(text)
        candidates = [i for i in items if i not in found]
        hit = next((i for i in candidates if normalize(i.text) == target), None)
        if hit is None and target:
            hit = next((i for i in candidates
                        if target in normalize(i.text) or normalize(i.text) in target), None)
        if hit is None:
            missing.append(text)
        else:
            found.append(hit)
    return found, missing
