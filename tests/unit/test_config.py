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
