"""Проверка initData из Telegram Mini App: Telegram подписывает данные о пользователе ключом бота,
поэтому паролей и своей авторизации не нужно — достаточно проверить подпись.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# initData живёт, пока открыт Mini App; сутки — с запасом, но старую подпись не примем
MAX_AGE_SECONDS = 24 * 3600


class InvalidInitData(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS,
                       now: float | None = None) -> dict:
    """Возвращает данные пользователя Telegram ({"id", "first_name", …}) или бросает InvalidInitData."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as e:
        raise InvalidInitData("malformed init data") from e
    received = pairs.pop("hash", "")
    if not received:
        raise InvalidInitData("no hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData("bad signature")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs["user"])
    except (ValueError, KeyError) as e:
        raise InvalidInitData("no user") from e
    if (now if now is not None else time.time()) - auth_date > max_age:
        raise InvalidInitData("expired")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InvalidInitData("no user id")
    return user


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Подписать initData так же, как Telegram, — для тестов и локальной отладки."""
    from urllib.parse import urlencode

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})
