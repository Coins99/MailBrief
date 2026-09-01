"""Tests for nonsecret application settings."""

import pytest
from pydantic import ValidationError

from mailbrief.config import Settings


def test_settings_read_mailbrief_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILBRIEF_MICROSOFT_CLIENT_ID", "client-id")
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "7")

    settings = Settings()

    assert settings.microsoft_client_id == "client-id"
    assert settings.ai_batch_size == 7


def test_settings_reject_invalid_batch_size() -> None:
    with pytest.raises(ValidationError):
        Settings(ai_batch_size=0)
