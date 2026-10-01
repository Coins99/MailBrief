"""CLI output and exit codes never expose credentials or mailbox contents."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mailbrief.config import Settings
from mailbrief.diagnostics import gmail
from mailbrief.domain.actions import ActionProposal, ProposalState
from mailbrief.domain.analysis import DeadlinePrecision, FollowUpKind, TargetReason
from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.digests import DigestCoverage, SyncResult, SyncStatus
from mailbrief.domain.drafting import DraftingOutcome, DraftingStatus
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderResponseError
from mailbrief.providers.gmail.auth import GmailAuth
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.services.calendar import InvalidTimezoneError
from mailbrief.services.preferences import PreferencesUnavailableError
from mailbrief.services.ranking import ExcludedSenderError, ShortlistReviewError
from tests.factories import make_action
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
        (ExcludedSenderError(), 3, "A message from an excluded sender can't be included"),
        (PreferencesUnavailableError(), 3, "Saved preferences could not be read."),
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


@pytest.mark.parametrize(
    ("fields", "line", "code"),
    [
        ({"status": BriefStatus.CANCELLED}, "Cancelled. No brief saved.", 130),
        ({"status": BriefStatus.CONSENT_DECLINED}, "Nothing was sent. No brief saved.", 6),
        (
            {"status": BriefStatus.SYNC_FAILED, "error_code": "RATE_LIMITED"},
            "Sync failed (RATE_LIMITED). Nothing was sent.",
            4,
        ),
        ({"status": BriefStatus.ANALYSIS_FAILED}, "No message could be analyzed.", 4),
        (
            # An automatic run whose analysis ran and failed two carried messages.
            {
                "status": BriefStatus.READY_FOR_REVIEW,
                "ready": 2,
                "needs_review": True,
                "coverage": DigestCoverage(
                    sync_complete=True, shortlisted=3, analyzed=1, reused=0, failed=2, skipped=0
                ),
            },
            "Today's brief needs your review: 2 messages from it couldn't be refreshed "
            "automatically.",
            4,
        ),
    ],
    ids=["cancelled", "declined", "sync-failed", "analysis-failed", "needs-review"],
)
def test_a_run_that_saved_no_brief_has_its_own_line_and_exit_code(
    fields: dict[str, Any], line: str, code: int
) -> None:
    sync = SyncResult(
        account_id="owner@example.com",
        range_start_utc=datetime(2026, 9, 4, 4, 0, tzinfo=UTC),
        range_end_utc=datetime(2026, 9, 5, 4, 0, tzinfo=UTC),
        status=SyncStatus.COMPLETE,
        page_count=1,
        message_count=1,
    )
    result = BriefRunResult(sync=sync, **fields)

    assert gmail._outcome(result) == line
    assert gmail._brief_exit_code(result) == code


@pytest.mark.parametrize(
    ("command", "code", "message"),
    [
        ("brief", 1, "Unexpected error (ValidationError)."),
        ("sync", 3, "Gmail configuration or secure storage is unavailable."),
    ],
)
def test_an_internal_validation_error_is_unexpected_only_in_ai_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    code: int,
    message: str,
) -> None:
    async def fail_validation(**options: Any) -> int:
        SyncResult.model_validate({})  # Raises pydantic's ValidationError.
        return 0

    monkeypatch.setattr(gmail, command, fail_validation)

    assert gmail.main([command]) == code
    assert capsys.readouterr().out.startswith(message)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DraftingStatus.DECLINED, 6),
        (DraftingStatus.CANCELLED, 130),
        (DraftingStatus.FAILED, 4),
    ],
)
def test_drafting_exit_codes_match_brief(status: DraftingStatus, code: int) -> None:
    outcome = DraftingOutcome(
        status=status, error_code="AI_TIMEOUT" if status is DraftingStatus.FAILED else None
    )
    assert gmail._drafting_exit_code(outcome) == code


def _proposal(**overrides: Any) -> ActionProposal:
    values: dict[str, Any] = {
        "id": 4,
        "action_public_id": "0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44",
        "action_title": "Send the deck",
        "action_revision": 1,
        "kind": FollowUpKind.CANCELLED,
        "state": ProposalState.PENDING,
        "evidence": "No longer\nneeded, thanks",
        "provider_message_id": "reply-1",
        "subject": "Re: deck",
        "sender_address": "sam@example.com",
        "received_at_utc": datetime(2026, 9, 30, 13, tzinfo=UTC),
        "web_link": "https://mail.google.com/mail/u/0/#inbox/reply-1",
        "created_at_utc": datetime(2026, 9, 30, 14, tzinfo=UTC),
    }
    values.update(overrides)
    return ActionProposal.model_validate(values)


@pytest.mark.parametrize("kind", [FollowUpKind.CANCELLED, FollowUpKind.DELIVERED])
def test_a_completing_proposal_reads_as_its_kind(kind: FollowUpKind) -> None:
    proposal = _proposal(kind=kind)
    action = make_action(title="Send the deck")
    zone = ZoneInfo("UTC")

    line = gmail._proposal_line(proposal, zone)
    applied = gmail._applied_line(proposal, action, action, zone)

    assert f" · {kind.value} · " in line
    assert '"No longer needed, thanks"' in line  # One line, whatever the quote held.
    assert applied == (
        f"Applied P4: completed Send the deck ({action.public_id}); the email says it was "
        f"{kind.value}."
    )


def test_a_new_deadline_keeps_a_target_the_owner_set() -> None:
    proposal = _proposal(
        kind=FollowUpKind.NEW_DEADLINE,
        deadline_text="next Monday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 5),
        deadline_timezone="UTC",
    )
    before = make_action(
        target_date=date(2026, 9, 30),
        suggested_target_date=date(2026, 10, 1),
        target_reason=TargetReason.WORKING_DAY_BEFORE,
    )
    after = before.model_copy(
        update={
            "deadline_text": "next Monday",
            "deadline_precision": DeadlinePrecision.DATE,
            "deadline_date": date(2026, 10, 5),
            "deadline_timezone": "UTC",
        }
    )

    line = gmail._applied_line(proposal, before, after, ZoneInfo("UTC"))

    assert line.endswith("now has deadline 2026-10-05; the target date you set is unchanged.")


def test_a_rank_reason_this_version_does_not_know_is_shown_as_stored() -> None:
    assert gmail._reason_words("tracked_thread_reply") == "reply in a thread you track"
    assert gmail._reason_words("from_the_future") == "from the future"
