import uuid

from gigachat import GigaChat
from gigachat.models import Chat, Function, FunctionCall, Messages, MessagesRole

from pim.llm.base import Message, ToolCall, ToolSpec

_ROLES = {
    "system": MessagesRole.SYSTEM,
    "user": MessagesRole.USER,
    "assistant": MessagesRole.ASSISTANT,
    "tool": MessagesRole.FUNCTION,
}


class GigaChatProvider:
    """GigaChat через официальный SDK.

    Отличия от OpenAI: за один ответ максимум один function_call, без id вызова;
    результат функции — сообщение role=function с name; functions_state_id надо возвращать обратно.
    """

    def __init__(self, credentials: str, scope: str, model: str, verify_ssl: bool, temperature: float = 0.2,
                 access_token: str = "", base_url: str = ""):
        self._client = GigaChat(
            credentials=credentials or None,
            access_token=access_token or None,
            base_url=base_url or None,
            scope=scope,
            model=model,
            verify_ssl_certs=verify_ssl,
        )
        self._temperature = temperature

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        payload = Chat(
            messages=[self._to_api(m) for m in messages],
            functions=[
                Function.model_validate({"name": t.name, "description": t.description, "parameters": t.parameters})
                for t in tools
            ]
            or None,
            function_call="auto" if tools else None,
            temperature=self._temperature,
        )
        resp = await self._client.achat(payload)
        msg = resp.choices[0].message
        calls = []
        if msg.function_call:
            calls.append(
                ToolCall(id=uuid.uuid4().hex, name=msg.function_call.name, arguments=msg.function_call.arguments or {})
            )
        meta = {"functions_state_id": msg.functions_state_id} if msg.functions_state_id else {}
        return Message(role="assistant", content=msg.content or "", tool_calls=calls, provider_meta=meta)

    @staticmethod
    def _to_api(m: Message) -> Messages:
        kwargs: dict = {"role": _ROLES[m.role], "content": m.content}
        if m.role == "tool":
            kwargs["name"] = m.name
        if m.tool_calls:
            tc = m.tool_calls[0]
            kwargs["function_call"] = FunctionCall(name=tc.name, arguments=tc.arguments)
        if fsid := m.provider_meta.get("functions_state_id"):
            kwargs["functions_state_id"] = fsid
        return Messages(**kwargs)
