import logging

from pim.config import Settings
from pim.stt.base import Transcriber

log = logging.getLogger(__name__)


def make_transcriber(settings: Settings) -> Transcriber | None:
    if settings.stt_provider == "none":
        return None
    if settings.stt_provider == "openrouter":
        if not settings.openrouter_api_key:
            log.warning("STT_PROVIDER=openrouter, но OPENROUTER_API_KEY пуст — голосовые выключены")
            return None
        from pim.stt.openrouter import OpenRouterTranscriber

        return OpenRouterTranscriber(settings.openrouter_api_key, settings.stt_model, settings.openrouter_base_url)
    if settings.stt_provider == "whisper":
        from pim.stt.whisper import WhisperTranscriber

        return WhisperTranscriber(
            model_size=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )
    raise ValueError(f"unknown STT provider: {settings.stt_provider}")
