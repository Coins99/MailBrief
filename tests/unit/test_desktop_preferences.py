"""Desktop configuration precedence, atomic writes and secret rejection."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from mailbrief.errors import ConfigurationError
from mailbrief.ui.preferences import DesktopPreferences, PreferencesStore


def test_saved_preferences_override_environment_and_keep_other_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILBRIEF_GROQ_MODEL", "environment-model")
    monkeypatch.setenv("MAILBRIEF_AI_MAX_REQUESTS_PER_RUN", "6")
    store = PreferencesStore(tmp_path / "settings.json")
    assert store.load().groq_model == "environment-model"
    saved = DesktopPreferences(
        groq_model="desktop-model", gmail_oauth_client_path=tmp_path / "x.json"
    )
    store.save(saved)
    restored = PreferencesStore(store.path).load()
    assert restored == saved
    assert restored.settings().groq_model == "desktop-model"
    assert restored.settings().ai_max_requests_per_run == 6
    store.save(DesktopPreferences())
    assert store.load().settings().groq_model is None


def test_failed_replace_preserves_previous_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PreferencesStore(tmp_path / "settings.json")
    store.save(DesktopPreferences(groq_model="old-model"))

    def fail(source: Path, destination: Path) -> None:
        raise OSError("PRIVATE_PATH_MARKER")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ConfigurationError) as error:
        store.save(DesktopPreferences(groq_model="new-model"))
    assert "PRIVATE_PATH_MARKER" not in str(error.value)
    assert store.load().groq_model == "old-model"
    assert list(tmp_path.iterdir()) == [store.path]


@pytest.mark.parametrize("raw", ["not json", '{"api_key":"PRIVATE_KEY_MARKER"}'])
def test_corrupt_and_secret_settings_are_rejected_without_echo(tmp_path: Path, raw: str) -> None:
    store = PreferencesStore(tmp_path / "settings.json")
    store.path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        store.load()
    assert "PRIVATE_KEY_MARKER" not in str(error.value)
    with pytest.raises(ValidationError):
        DesktopPreferences.model_validate({"api_key": "PRIVATE_KEY_MARKER"})
