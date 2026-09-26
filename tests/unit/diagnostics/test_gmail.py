"""CLI output and exit codes never expose credentials or mailbox contents."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from mailbrief.config import Settings
from mailbrief.diagnostics import gmail
from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.digests import SyncResult, SyncStatus
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderResponseError
from mailbrief.providers.gmail.auth import GmailAuth
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.services.calendar import InvalidTimezoneError
from mailbrief.services.ranking import ShortlistReviewError
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
    "error,code,message",
    [
        (AuthenticationRequiredError("Reconnect Gmail."), 2, "Reconnect Gmail."),
        (ConfigurationError("sensitive file path"), 3, "configuration or secure storage"),
        (ProviderResponseError("Google unavailable."), 4, "Google unavailable."),
        (InvalidTimezoneError("sensitive zone"), 3, "Invalid timezone or shortlist choices"),
        (ShortlistReviewError("sensitive IDs"), 3, "Invalid timezone or shortlist choices"),
        (ValueError("sensitive detail"), 1, "Unexpected error (ValueError)."),
        (KeyboardInterrupt(), 130, "Cancelled."),
    ],
)
def test_safe_failure_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    code: int,
    message: str,
) -> None:
    @asynccontextmanager
    async def factory(settings: Settings) -> AsyncIterator[GmailAuth]:
        raise error
        yield  # type: ignore[unreachable]

    monkeypatch.setattr(gmail, "gmail_auth", factory)
    assert gmail.main(["fetch", "--silent-only"]) == code
    output = capsys.readouterr().out
    assert message in output
    assert "sensitive" not in output


def test_invalid_settings_are_named_without_their_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MAILBRIEF_AI_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "999")

    assert gmail.main(["fetch"]) == 3

    output = capsys.readouterr().out
    assert output.strip() == (
        "Invalid setting: MAILBRIEF_AI_BATCH_SIZE, MAILBRIEF_AI_TIMEOUT_SECONDS. "
        "See docs/ai-analysis.md."
    )


async def test_a_prompt_answer_is_stripped_and_eof_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str | EOFError] = [" y ", EOFError()]

    def fake_input(prompt: str = "") -> str:
        answer = answers.pop(0)
        if isinstance(answer, EOFError):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)

    assert await gmail._ask("Send? ") == "y"
    assert await gmail._ask("Send? ") == ""


async def test_a_cancelled_prompt_does_not_wait_for_input(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()

    def blocking_input(prompt: str = "") -> str:
        started.set()
        release.wait(5)
        return "too late"

    monkeypatch.setattr("builtins.input", blocking_input)
    task = asyncio.create_task(gmail._ask("Send? "))
    await asyncio.to_thread(started.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (reader,) = [thread for thread in threading.enumerate() if thread.name == "mailbrief-prompt"]
    assert reader.daemon  # Interpreter exit never waits for the pending read.
    release.set()
    reader.join(5)


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


def test_a_key_missing_failure_keeps_the_saved_brief_note_on_its_own_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sync = SyncResult(
        account_id="owner@example.com",
        range_start_utc=datetime(2026, 9, 4, 4, 0, tzinfo=UTC),
        range_end_utc=datetime(2026, 9, 5, 4, 0, tzinfo=UTC),
        status=SyncStatus.COMPLETE,
        page_count=1,
        message_count=1,
    )
    result = BriefRunResult(
        status=BriefStatus.ANALYSIS_FAILED, sync=sync, error_code="AI_KEY_MISSING"
    )

    gmail._print_result(result, model="test-model")

    lines = capsys.readouterr().out.splitlines()
    assert lines[-2:] == [
        "No usable Groq API key is saved. Run: mailbrief-gmail-diagnostic ai-key set",
        "Your last saved brief for today is unchanged.",
    ]
