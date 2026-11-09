"""Поиск близкого по тому, как его назвали: «Андрей», «Андрею», «Андрея Петрова»."""

from typing import Sequence

from pim.core.models import Connection, User
from pim.lists.matching import normalize

# Русские падежи меняют одну-две последние буквы: «Андрей / Андрею», «Мама / Маме», «Савва / Савве»
MIN_COMMON = 3


def _same_word(a: str, b: str) -> bool:
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= MIN_COMMON and common >= min(len(a), len(b)) - 2


def _names(conn: Connection, peer: User) -> list[str]:
    names = [normalize(conn.alias)]
    if peer.name:
        names.append(normalize(peer.name))
    # «Андрей Петров» -> ещё и «андрей»
    return names + [n.split()[0] for n in names if " " in n]


def find_person(people: Sequence[tuple[Connection, User]], name: str) -> tuple[Connection, User] | None:
    """Точное совпадение имени, иначе — единственный близкий с тем же словом в другом падеже. Неоднозначно — None."""
    target = normalize(name)
    if not target:
        return None
    first = target.split()[0]
    for conn, peer in people:
        if target in _names(conn, peer) or first in _names(conn, peer):
            return conn, peer
    same = [(c, p) for c, p in people if any(_same_word(first, n.split()[0]) for n in _names(c, p))]
    return same[0] if len(same) == 1 else None
