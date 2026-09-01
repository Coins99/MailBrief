"""Validated nonsecret application configuration."""

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from ``MAILBRIEF_`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="MAILBRIEF_", extra="ignore")

    microsoft_client_id: str | None = None
    graph_base_url: HttpUrl = HttpUrl("https://graph.microsoft.com/v1.0")
    openai_model: str | None = None
    ai_batch_size: int = Field(default=5, ge=1, le=10)
    ai_body_character_limit: int = Field(default=8_000, ge=1, le=20_000)
