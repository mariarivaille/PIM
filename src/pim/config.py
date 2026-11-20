from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    allowed_user_ids: Annotated[list[int], NoDecode] = []

    llm_provider: Literal["openrouter", "gigachat"] = "gigachat"

    openrouter_api_key: str = ""
    openrouter_model: str = "qwen/qwen3-235b-a22b-2507"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Предел длины ответа: без него OpenRouter резервирует максимум модели (65k) и отказывает при малом балансе
    openrouter_max_tokens: int = 2048
    # Поиск в интернете (инструмент web_search) — через OpenRouter, нужен OPENROUTER_API_KEY; off — выключить
    web_search: Literal["openrouter", "off"] = "openrouter"
    web_search_model: str = ""  # пусто — та же, что OPENROUTER_MODEL
    web_search_results: int = 3
    web_search_per_turn: int = 3  # предел поисков за одно сообщение пользователя — каждый поиск платный

    gigachat_credentials: str = ""
    # Готовый access-токен вместо ключа авторизации (живёт ограниченное время, бот его не обновляет)
    gigachat_access_token: str = ""
    # Адрес API; пусто = по умолчанию SDK
    gigachat_base_url: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_verify_ssl: bool = False

    # Распознавание голосовых: openrouter (мультимодальная модель, нужен OPENROUTER_API_KEY) | whisper (локально,
    # ~1.5 ГБ RAM, `pip install -e .[whisper]`) | none (выключено)
    stt_provider: Literal["openrouter", "whisper", "none"] = "openrouter"
    stt_model: str = "google/gemini-3.5-flash-lite"
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_cpu_threads: int = 4
    max_voice_seconds: int = 300
    # Telegram Mini App (pim/web): публичный HTTPS-адрес; пусто — Mini App и API выключены
    webapp_url: str = ""
    web_host: str = "127.0.0.1"  # наружу API выставляет Caddy, сам сервер слушает только локально
    web_port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./pim.db"
    default_timezone: str = "Europe/Moscow"
    reminder_poll_seconds: int = 30
    history_size: int = 10
    # После такой паузы история диалога забывается
    history_ttl_minutes: int = 60
    # Через сколько минут тишины бот разбирает разговор и сам запоминает важное
    reflection_idle_minutes: int = 10
    # Максимум фактов в памяти на пользователя (вся память идёт в промпт)
    max_memories: int = 150
    log_level: str = "INFO"

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.split(",") if x.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
