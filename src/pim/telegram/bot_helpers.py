"""Мелочи, нужные и bot.py, и handlers.py (handlers не может импортировать bot — тот импортирует handlers)."""

INVITE_PREFIX = "join_"


def invite_code(text: str) -> str | None:
    """Код приглашения из «/start join_<код>»."""
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].startswith("/start") and parts[1].startswith(INVITE_PREFIX):
        return parts[1][len(INVITE_PREFIX):].strip()
    return None
