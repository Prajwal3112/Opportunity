from functools import lru_cache

from pydantic import HttpUrl, PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://worldbank:worldbank@localhost:5432/worldbank_opportunities"
    world_bank_base_url: HttpUrl = "https://search.worldbank.org"
    world_bank_timeout_seconds: PositiveFloat = 30
    world_bank_max_retries: PositiveInt = 3
    world_bank_backoff_seconds: PositiveFloat = 0.5
    # Documents are multi-MB PDFs; the API timeout is far too short for them.
    world_bank_document_timeout_seconds: PositiveFloat = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
