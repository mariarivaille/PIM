"""Распознавание голосовых через мультимодальную модель на OpenRouter (по умолчанию Gemini Flash Lite).

Вместо локального Whisper: тот требует ~1.5 ГБ памяти и заметно грузит слабый CPU, а здесь минута голоса стоит
доли цента и распознаётся за пару секунд. Аудио уходит как input_audio (base64) — OGG/Opus из Telegram модель
принимает как есть.
"""

import base64
import logging
import time

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

PROMPT = (
    "Дословно расшифруй это голосовое сообщение на русском языке. Верни только текст сказанного, без "
    "комментариев, кавычек и пометок. Расставь знаки препинания, числа и время пиши цифрами («в 19:00», "
    "«26 ноября»). Если речи нет или ничего не разобрать — верни пустую строку."
)

# mime из Telegram → формат input_audio, который понимает OpenRouter
FORMATS = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/flac": "flac", "audio/aac": "aac", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/m4a": "m4a", "video/mp4": "m4a",
}


def audio_format(mime: str | None) -> str:
    return FORMATS.get((mime or "").split(";")[0].strip().lower(), "ogg")


class OpenRouterTranscriber:
    def __init__(self, api_key: str, model: str, base_url: str, max_tokens: int = 2000):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens

    async def transcribe(self, audio: bytes, mime: str | None = None) -> str:
        started = time.monotonic()
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio",
                 "input_audio": {"data": base64.b64encode(audio).decode(), "format": audio_format(mime)}},
            ]}],
            max_tokens=self._max_tokens,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip().strip("«»\"")
        log.info("transcribed %d bytes via %s in %.1fs", len(audio), self._model, time.monotonic() - started)
        return text

    async def warmup(self) -> None:
        return None
