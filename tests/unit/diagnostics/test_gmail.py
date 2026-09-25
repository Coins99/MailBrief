"""CLI output and exit codes never expose credentials or mailbox contents."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from mailbrief.config import Settings
from mailbrief.diagnostics import gmail
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderResponseError
from mailbrief.providers.gmail.auth import GmailAuth
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from tests.unit.providers.gmail.test_cache import MemoryVault, credential


@pytest.mark.parametrize("silent", [True, False])
def test_fetch_passes_silent_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], silent: bool
) -> None:
    calls: list[bool] = []

    class FakeAuth:
        async def connect(self, *, silent_only: bool = False) -> None:
            calls.append(silent_only)

    @asynccontextmanager
    async def factory(settings: Settings) -> AsyncIterator[FakeAuth]:
        yield FakeAuth()

    monkeypatch.setattr(gmail, "gmail_auth", factory)
    assert gmail.main(["fetch", *(["--silent-only"] if silent else [])]) == 0
    assert calls == [silent]
    assert "No messages downloaded" in capsys.readouterr().out


def test_disconnect_needs_no_client_file(monkeypatch: pytest.MonkeyPatch) -> None:
    store = GmailCredentialStore(MemoryVault())
    store.save(credential())
    monkeypatch.setattr(gmail, "GmailCredentialStore", lambda: store)
    assert gmail.main(["disconnect"]) == 0
    assert store.load() is None


@pytest.mark.parametrize(
    "error,code",
    [
        (AuthenticationRequiredError("Reconnect Gmail."), 2),
        (ConfigurationError("sensitive file path"), 3),
        (ProviderResponseError("Google unavailable."), 4),
        (KeyboardInterrupt(), 130),
    ],
)
def test_safe_failure_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    code: int,
) -> None:
    @asynccontextmanager
    async def factory(settings: Settings) -> AsyncIterator[GmailAuth]:
        raise error
        yield  # type: ignore[unreachable]

    monkeypatch.setattr(gmail, "gmail_auth", factory)
    assert gmail.main(["fetch", "--silent-only"]) == code
    assert "sensitive" not in capsys.readouterr().out


def test_actionable_setup_error_is_displayed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @asynccontextmanager
    async def factory(settings: Settings) -> AsyncIterator[GmailAuth]:
        raise GmailSetupError("OAuth client file not found. Check the configured path.")
        yield  # type: ignore[unreachable]

    monkeypatch.setattr(gmail, "gmail_auth", factory)
    assert gmail.main(["fetch"]) == 3
    assert "OAuth client file not found" in capsys.readouterr().out


def test_missing_path_is_actionable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH", raising=False)
    assert gmail.main(["fetch", "--silent-only"]) == 3
    assert "Set MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH" in capsys.readouterr().out
