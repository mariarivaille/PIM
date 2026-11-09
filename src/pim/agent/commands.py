"""Команды, которые выполняются без LLM: точные и мгновенные."""

import re

# «удали факт 3», «удали факты 3, 5 и 7», «забудь факт №2.» — в том числе из распознанного голосового
DELETE_FACTS = re.compile(
    r"^\s*(?:удали(?:ть)?|забудь|сотри)\s+факт(?:ы)?\s*"
    r"((?:№|#)?\s*\d+(?:\s*(?:,|и)\s*(?:№|#)?\s*\d+)*)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def parse_delete_facts(text: str) -> list[int] | None:
    """Номера фактов из команды удаления или None, если это не она."""
    m = DELETE_FACTS.match(text)
    if not m:
        return None
    return list(dict.fromkeys(int(n) for n in re.findall(r"\d+", m.group(1))))
