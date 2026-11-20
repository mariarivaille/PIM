from pim.config import Settings
from pim.stt.factory import make_transcriber
from pim.stt.openrouter import OpenRouterTranscriber, audio_format


def test_audio_format():
    assert audio_format("audio/ogg") == "ogg"
    assert audio_format("audio/mpeg") == "mp3"
    assert audio_format("video/mp4") == "m4a"  # кружок
    assert audio_format(None) == "ogg"  # голосовое Telegram


def test_factory():
    assert make_transcriber(Settings(_env_file=None, stt_provider="none")) is None
    assert make_transcriber(Settings(_env_file=None, stt_provider="openrouter", openrouter_api_key="")) is None
    t = make_transcriber(Settings(_env_file=None, stt_provider="openrouter", openrouter_api_key="sk-test"))
    assert isinstance(t, OpenRouterTranscriber)
