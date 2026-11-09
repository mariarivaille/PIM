from typing import Protocol


class Transcriber(Protocol):
    """Распознавание речи. Реализации: локальный Whisper, позже — SaluteSpeech и т.п."""

    async def transcribe(self, audio: bytes) -> str:
        """Аудио в любом формате, который понимает реализация (Telegram отдаёт OGG/Opus) -> текст."""
        ...

    async def warmup(self) -> None:
        """Заранее подготовить модель, чтобы первое голосовое не ждало загрузку."""
        ...
