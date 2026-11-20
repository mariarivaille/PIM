from typing import Protocol


class Transcriber(Protocol):
    """Распознавание речи. Реализации: модель на OpenRouter (stt/openrouter.py), локальный Whisper."""

    async def transcribe(self, audio: bytes, mime: str | None = None) -> str:
        """Аудио (Telegram отдаёт голосовые в OGG/Opus, кружки — MP4) -> текст. mime — тип файла из Telegram."""
        ...

    async def warmup(self) -> None:
        """Заранее подготовить модель, чтобы первое голосовое не ждало загрузку."""
        ...
