import asyncio
import io
import logging
import time

log = logging.getLogger(__name__)


class WhisperTranscriber:
    """Локальный Whisper через faster-whisper (CTranslate2). OGG/Opus из Telegram декодирует сам через PyAV.

    Модель грузится лениво (при первом вызове или warmup) — при первом запуске она скачивается с HF.
    Распознавания выполняются по одному: модель тяжёлая, параллельные вызовы только мешают друг другу.
    """

    def __init__(self, model_size: str, device: str = "cpu", compute_type: str = "int8",
                 cpu_threads: int = 4, language: str = "ru"):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._language = language
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            started = time.monotonic()
            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type, cpu_threads=self._cpu_threads
            )
            log.info("whisper model %s loaded in %.1fs", self._model_size, time.monotonic() - started)
        return self._model

    def _transcribe_sync(self, audio: bytes) -> str:
        model = self._load()
        started = time.monotonic()
        segments, info = model.transcribe(
            io.BytesIO(audio), language=self._language, vad_filter=True, beam_size=5
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("transcribed %.1fs of audio in %.1fs", info.duration, time.monotonic() - started)
        return text

    async def transcribe(self, audio: bytes) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, audio)

    async def warmup(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load)
