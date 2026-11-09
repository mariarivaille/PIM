import json
import logging

from openai import AsyncOpenAI

from aid.llm.base import Message, ToolCall, ToolSpec

log = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Любой OpenAI-совместимый API: OpenRouter (qwen, gemini, ...), vLLM, локальные сервера."""

    def __init__(self, api_key: str, model: str, base_url: str, temperature: float = 0.2):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._temperature = temperature

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[self._to_api(m) for m in messages],
            tools=[
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            or None,
            temperature=self._temperature,
        )
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                log.warning("bad tool arguments from model: %r", tc.function.arguments)
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return Message(role="assistant", content=msg.content or "", tool_calls=calls)

    @staticmethod
    def _to_api(m: Message) -> dict:
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        d: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in m.tool_calls
            ]
        return d
