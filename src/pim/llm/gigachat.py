import asyncio
import hashlib
import logging
import ssl
import uuid

import httpx

from gigachat import GigaChat
from gigachat.models import Chat, Function, FunctionCall, Messages, MessagesRole

from pim.llm.base import Image, Message, ToolCall, ToolSpec

log = logging.getLogger(__name__)

NETWORK_RETRIES = 1

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
    Картинки сначала загружаются файлом, в сообщение идёт их id (attachments).
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
        # sha256 картинки -> id загруженного файла: в цикле агента одно и то же сообщение уходит несколько раз
        self._uploaded: dict[str, str] = {}

    async def _attachments(self, images: list[Image]) -> list[str]:
        ids = []
        for i, img in enumerate(images):
            key = hashlib.sha256(img.data).hexdigest()
            if key not in self._uploaded:
                ext = img.mime.split("/")[-1]
                uploaded = await self._client.aupload_file((f"image{i}.{ext}", img.data, img.mime))
                self._uploaded[key] = uploaded.id_
            ids.append(self._uploaded[key])
        return ids

    async def cleanup(self, images: list[Image]) -> None:
        """Удаляет картинки этого хода из хранилища GigaChat — фото пользователя там не копятся.

        Только переданные: провайдер общий, параллельно могут идти ходы других пользователей.
        """
        for img in images:
            file_id = self._uploaded.pop(hashlib.sha256(img.data).hexdigest(), None)
            if file_id is None:
                continue
            try:
                await self._client.adelete_file(file_id)
            except Exception:
                log.warning("failed to delete uploaded file %s", file_id, exc_info=True)

    async def _achat_with_retry(self, payload: Chat):
        """Сетевые сбои (обрыв SSL, сброс соединения) SDK не повторяет — повторяем сами, один раз."""
        for attempt in range(NETWORK_RETRIES + 1):
            try:
                return await self._client.achat(payload)
            except (httpx.TransportError, ssl.SSLError) as e:
                if attempt == NETWORK_RETRIES:
                    raise
                log.warning("GigaChat network error, retrying: %s", e)
                await asyncio.sleep(1)

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        payload = Chat(
            messages=[self._to_api(m, await self._attachments(m.images) if m.images else None) for m in messages],
            functions=[
                Function.model_validate({"name": t.name, "description": t.description, "parameters": t.parameters})
                for t in tools
            ]
            or None,
            function_call="auto" if tools else None,
            temperature=self._temperature,
        )
        resp = await self._achat_with_retry(payload)
        msg = resp.choices[0].message
        calls = []
        if msg.function_call:
            calls.append(
                ToolCall(id=uuid.uuid4().hex, name=msg.function_call.name, arguments=msg.function_call.arguments or {})
            )
        meta = {"functions_state_id": msg.functions_state_id} if msg.functions_state_id else {}
        return Message(role="assistant", content=msg.content or "", tool_calls=calls, provider_meta=meta)

    @staticmethod
    def _to_api(m: Message, attachments: list[str] | None = None) -> Messages:
        kwargs: dict = {"role": _ROLES[m.role], "content": m.content}
        if attachments:
            kwargs["attachments"] = attachments
        if m.role == "tool":
            kwargs["name"] = m.name
        if m.tool_calls:
            tc = m.tool_calls[0]
            kwargs["function_call"] = FunctionCall(name=tc.name, arguments=tc.arguments)
        if fsid := m.provider_meta.get("functions_state_id"):
            kwargs["functions_state_id"] = fsid
        return Messages(**kwargs)
