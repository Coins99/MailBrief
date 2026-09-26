"""Tests for nonsecret application settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mailbrief.config import Settings
from mailbrief.domain.messages import ProviderKind
from mailbrief.errors import ConfigurationError


def test_settings_read_mailbrief_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILBRIEF_EMAIL_PROVIDER", "microsoft")
    monkeypatch.setenv("MAILBRIEF_MICROSOFT_CLIENT_ID", "client-id")
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "7")

    settings = Settings()

    assert settings.email_provider is ProviderKind.MICROSOFT
    assert settings.microsoft_client_id == "client-id"
    assert settings.ai_batch_size == 7


def test_gmail_is_the_default_email_provider() -> None:
    assert Settings().email_provider is ProviderKind.GMAIL


def test_gmail_oauth_path_is_required_only_on_demand(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH"):
        Settings().require_gmail_oauth_client_path()

    configured = tmp_path / "google-oauth.json"
    assert (
        Settings(gmail_oauth_client_path=configured).require_gmail_oauth_client_path() == configured
    )


def test_settings_reject_invalid_batch_size() -> None:
    with pytest.raises(ValidationError):
        Settings(ai_batch_size=0)


def test_microsoft_client_id_is_required_only_on_demand() -> None:
    settings = Settings(microsoft_client_id=None)
    with pytest.raises(ConfigurationError, match="MAILBRIEF_MICROSOFT_CLIENT_ID"):
        settings.require_microsoft_client_id()
    assert (
        Settings(microsoft_client_id="  client-id  ").require_microsoft_client_id() == "client-id"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://graph.microsoft.com/v1.0",
        "https://evil.example/v1.0",
        "https://user@graph.microsoft.com/v1.0",
        "https://graph.microsoft.com/beta",
        "https://graph.microsoft.com/v1.0?redirect=evil",
    ],
)
def test_settings_reject_unsafe_graph_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(graph_base_url=url)


def test_settings_accept_public_graph_url_with_trailing_slash() -> None:
    settings = Settings(graph_base_url="https://graph.microsoft.com/v1.0/")
    assert str(settings.graph_base_url) == "https://graph.microsoft.com/v1.0/"


def test_body_limit_cannot_exceed_the_analysis_limit() -> None:
    assert Settings().ai_body_character_limit == 4_000
    with pytest.raises(ValidationError):
        Settings(ai_body_character_limit=8_001)


@pytest.mark.parametrize("model", [None, "", "   "])
def test_groq_model_is_required_only_on_demand(model: str | None) -> None:
    with pytest.raises(ConfigurationError, match="MAILBRIEF_GROQ_MODEL"):
        Settings(groq_model=model).require_groq_model()


def test_groq_model_is_returned_stripped() -> None:
    assert Settings(groq_model=" gpt-test ").require_groq_model() == "gpt-test"


@pytest.mark.parametrize(
    "values",
    [
        {"ai_max_output_tokens": 255},
        {"ai_max_output_tokens": 64_001},
        {"ai_max_requests_per_run": 0},
        {"ai_max_requests_per_run": 1_001},
        {"ai_timeout_seconds": 9.9},
        {"ai_timeout_seconds": 600.1},
    ],
)
def test_ai_limits_are_bounded(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_ai_limits_accept_their_bounds() -> None:
    low = Settings(ai_max_output_tokens=256, ai_timeout_seconds=10)
    high = Settings(ai_max_output_tokens=64_000, ai_timeout_seconds=600)

    assert (low.ai_max_output_tokens, low.ai_timeout_seconds) == (256, 10)
    assert (high.ai_max_output_tokens, high.ai_timeout_seconds) == (64_000, 600)
