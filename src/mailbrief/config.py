"""Validated nonsecret application configuration."""

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mailbrief.errors import ConfigurationError


class Settings(BaseSettings):
    """Configuration loaded from ``MAILBRIEF_`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="MAILBRIEF_", extra="ignore")

    microsoft_client_id: str | None = None
    graph_base_url: HttpUrl = HttpUrl("https://graph.microsoft.com/v1.0")
    openai_model: str | None = None
    ai_batch_size: int = Field(default=5, ge=1, le=10)
    ai_body_character_limit: int = Field(default=8_000, ge=1, le=20_000)

    @field_validator("graph_base_url")
    @classmethod
    def validate_graph_base_url(cls, value: HttpUrl) -> HttpUrl:
        """Prevent configuration from redirecting Microsoft bearer tokens."""
        if (
            value.scheme != "https"
            or value.host != "graph.microsoft.com"
            or value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
            or value.port not in {None, 443}
            or (value.path or "").rstrip("/") != "/v1.0"
        ):
            raise ValueError("Microsoft Graph URL must be the public v1.0 HTTPS endpoint")
        return value

    def require_microsoft_client_id(self) -> str:
        """Return configured Microsoft client ID or an actionable error."""
        value = (self.microsoft_client_id or "").strip()
        if not value:
            raise ConfigurationError(
                "Microsoft integration is not configured; set MAILBRIEF_MICROSOFT_CLIENT_ID."
            )
        return value
