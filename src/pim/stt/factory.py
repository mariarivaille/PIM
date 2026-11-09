from pim.config import Settings
from pim.stt.base import Transcriber


def make_transcriber(settings: Settings) -> Transcriber | None:
    if settings.stt_provider == "none":
        return None
    if settings.stt_provider == "whisper":
        from pim.stt.whisper import WhisperTranscriber

        return WhisperTranscriber(
            model_size=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )
    raise ValueError(f"unknown STT provider: {settings.stt_provider}")
