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

    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_verify_ssl: bool = False

    database_url: str = "sqlite+aiosqlite:///./aid.db"
    default_timezone: str = "Europe/Moscow"
    reminder_poll_seconds: int = 30
    history_size: int = 10
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
