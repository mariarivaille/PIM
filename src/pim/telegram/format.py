"""Ответ модели -> HTML для Telegram. Модели пишут markdown (особенно после поиска: ссылки, жирный, списки),
а бот шлёт в режиме HTML — без перевода в чате видны звёздочки и скобки."""

import html
import re

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_BULLET = re.compile(r"^(\s*)[*-]\s+", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s*(.+)$", re.MULTILINE)


def md_to_html(text: str) -> str:
    links: list[str] = []

    def keep_link(m: re.Match) -> str:
        links.append(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>')
        return f"\x00{len(links) - 1}\x00"

    text = _LINK.sub(keep_link, text)  # ссылки — до экранирования, чтобы не испортить url
    text = html.escape(text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _BULLET.sub(r"\1• ", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], text)
