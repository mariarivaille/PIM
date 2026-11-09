"""Провайдеро-независимый формат сообщений и tool calling.

Агент работает только с этими типами; адаптеры (openrouter, gigachat) переводят их в формат своего API.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    # JSON Schema объекта аргументов. Держим простым (string/integer/enum, без anyOf/null) —
    # GigaChat понимает только базовое подмножество.
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # для role="tool": на какой вызов это ответ
    tool_call_id: str | None = None
    name: str | None = None
    # непрозрачные данные провайдера, которые надо вернуть ему в следующем запросе
    # (например functions_state_id у GigaChat)
    provider_meta: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        """Возвращает ответ ассистента: текст и/или tool_calls."""
        ...
