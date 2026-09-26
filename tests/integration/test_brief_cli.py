"""The brief, ai-key and ai-consent commands end to end, with Gmail and OpenAI over respx."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from mailbrief.config import Settings
from mailbrief.diagnostics import gmail
from mailbrief.providers.gmail.client import MESSAGES_URL, GmailClient
from mailbrief.providers.gmail.provider import GmailProvider
from mailbrief.providers.openai.credentials import ENTRY, SERVICE
from tests.unit.providers.gmail.body_fixtures import message, part
from tests.unit.providers.gmail.metadata_fixtures import FakeSession, metadata
from tests.unit.providers.openai.openai_fixtures import (
    RESPONSES_URL,
    TEST_KEY,
    MemoryVault,
    answer_every_message,
    error_body,
    results_body,
    sent_messages,
    wire_result,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

MARKER = "BRIEF-BODY-MARKER-31f7"
BODY = f"Please approve the quarterly budget by Friday. {MARKER} must stay private."
EVIDENCE = BODY[:40]
Responder = Callable[[httpx.Request], httpx.Response]


class Mailbox:
    """Synthetic Inbox messages over respx; a test can change the body or add a message."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.body = BODY
        self._router = router
        self._identifiers: list[str] = []
        self._metadata: dict[str, respx.Route] = {}
        router.get(MESSAGES_URL).mock(side_effect=self._list)
        self.add("a1")

    def add(self, identifier: str) -> None:
        received = datetime.now(UTC)
        self._identifiers.append(identifier)
        self._router.get(f"{MESSAGES_URL}/{identifier}", params__contains={"format": "full"}).mock(
            side_effect=lambda request: self._full_message(identifier)
        )
        self._metadata[identifier] = self._router.get(f"{MESSAGES_URL}/{identifier}").respond(
            json=metadata(identifier, received=received)
        )

    def break_metadata(self, identifier: str) -> None:
        """Serve another message's metadata, which the sync counts as a failed item."""
        self._metadata[identifier].respond(json=metadata("mismatched"))

    def _list(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": id_} for id_ in self._identifiers]})

    def _full_message(self, identifier: str) -> httpx.Response:
        body = message(part("text/plain", self.body), identifier=identifier)
        return httpx.Response(200, json=body)


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> MemoryVault:
    backend = MemoryVault({(SERVICE, ENTRY): TEST_KEY})
    monkeypatch.setattr("mailbrief.providers.openai.credentials.os_vault", lambda: backend)
    return backend


@pytest.fixture
def mailbox(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, vault: MemoryVault
) -> Mailbox:
    @asynccontextmanager
    async def factory(
        settings: Settings, *, silent_only: bool = False
    ) -> AsyncIterator[GmailProvider]:
        async with httpx.AsyncClient() as http:
            yield GmailProvider(
                FakeSession(), GmailClient(http, FakeSession()), silent_only=silent_only
            )

    monkeypatch.setattr(gmail, "gmail_provider", factory)
    monkeypatch.setenv("MAILBRIEF_OPENAI_MODEL", "test-model")
    return Mailbox(respx_mock)


def openai_answers(
    router: respx.MockRouter, responder: Responder = answer_every_message
) -> respx.Route:
    return router.post(RESPONSES_URL).mock(side_effect=responder)


def replies(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    """Answer input() prompts in order; an unexpected prompt fails the test."""
    queue = list(answers)

    def fake_input(prompt: str = "") -> str:
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def run_brief(path: Path, *options: str) -> int:
    arguments = ["brief", "--silent-only", "--database", str(path), "--timezone", "UTC"]
    return gmail.main([*arguments, *options])


def test_first_brief_asks_then_saves_and_prints_counts_only(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = openai_answers(respx_mock)
    replies(monkeypatch, "yes")
    path = tmp_path / "brief.sqlite3"

    assert run_brief(path) == 0

    output = capsys.readouterr().out
    assert "MailBrief will send 1 message to OpenAI (test-model)" in output
    assert "Brief: saved (complete); items: 1" in output
    assert (
        "Coverage: shortlisted 1, analyzed 1, reused 0, failed 0, skipped 0; sync complete: yes"
        in output
    )
    assert "AI: OpenAI / test-model; requests: 1; tokens in/out: 1200 / 300" in output
    assert "Saved. Bodies were not stored." in output
    for private in ("Approval needed", "sender@example.com", EVIDENCE, MARKER):
        assert private not in output
    assert route.call_count == 1
    assert gmail.main(["ai-consent", "status", "--database", str(path)]) == 0
    assert "OpenAI consent granted" in capsys.readouterr().out


def test_a_repeat_brief_with_yes_reuses_everything(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = openai_answers(respx_mock)
    replies(monkeypatch, "yes")
    path = tmp_path / "brief.sqlite3"
    assert run_brief(path) == 0
    capsys.readouterr()

    assert run_brief(path, "--yes") == 0

    output = capsys.readouterr().out
    assert route.call_count == 1
    assert "analyzed 0, reused 1, failed 0" in output
    assert "AI: nothing sent this run" in output
    assert "MailBrief will send" not in output


@pytest.mark.parametrize("answer", ["no", "", "y", "YES"])
def test_first_use_needs_exactly_yes(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    route = openai_answers(respx_mock)
    replies(monkeypatch, answer)

    assert run_brief(tmp_path / "brief.sqlite3") == 6

    assert route.call_count == 0
    assert "Nothing was sent. No brief saved." in capsys.readouterr().out


def test_yes_never_grants_first_use_consent(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = openai_answers(respx_mock)
    replies(monkeypatch)

    assert run_brief(tmp_path / "brief.sqlite3", "--yes") == 6

    assert route.call_count == 0
    output = capsys.readouterr().out
    assert "First use needs your interactive consent; run brief without --yes." in output


def test_revoked_consent_blocks_a_later_brief_with_yes(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = openai_answers(respx_mock)
    replies(monkeypatch, "yes")
    path = tmp_path / "brief.sqlite3"
    assert run_brief(path) == 0
    assert gmail.main(["ai-consent", "revoke", "--database", str(path)]) == 0
    assert "OpenAI consent revoked: 1" in capsys.readouterr().out
    mailbox.body = f"{BODY} A follow-up line arrived."

    assert run_brief(path, "--yes") == 6

    assert route.call_count == 1
    assert "First use needs your interactive consent" in capsys.readouterr().out
    assert gmail.main(["ai-consent", "status", "--database", str(path)]) == 0
    assert "OpenAI consent not granted" in capsys.readouterr().out


def test_a_rejected_key_exits_4_with_the_ai_key_hint(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = respx_mock.post(RESPONSES_URL).respond(401, json=error_body("invalid_api_key"))
    replies(monkeypatch, "yes")

    assert run_brief(tmp_path / "brief.sqlite3") == 4

    output = capsys.readouterr().out
    assert route.call_count == 1
    assert "Brief: analysis_failed; items: 0" in output
    assert "AI: OpenAI / test-model; requests: 1; tokens in/out: ? / ?" in output
    assert "nothing sent" not in output
    assert (
        "OpenAI rejected the API key. Run: mailbrief-gmail-diagnostic ai-key set "
        "Your last saved brief for today is unchanged."
    ) in output


def test_a_partial_brief_names_the_provider_error(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = openai_answers(respx_mock)
    replies(monkeypatch, "yes")
    path = tmp_path / "brief.sqlite3"
    assert run_brief(path) == 0
    capsys.readouterr()
    mailbox.add("a2")
    route.mock(return_value=httpx.Response(401, json=error_body("invalid_api_key")))

    assert run_brief(path, "--yes") == 4

    lines = capsys.readouterr().out.splitlines()
    assert route.call_count == 2
    assert "Brief: saved (partial); items: 1" in lines
    assert any("analyzed 0, reused 1, failed 1" in line for line in lines)
    assert "AI: OpenAI / test-model; requests: 1; tokens in/out: ? / ?" in lines
    assert not any("nothing sent" in line for line in lines)
    outcome = lines.index("Saved. Bodies were not stored.")
    assert lines[outcome + 1] == (
        "OpenAI rejected the API key. Run: mailbrief-gmail-diagnostic ai-key set"
    )


def test_an_empty_brief_after_an_incomplete_sync_exits_4(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = openai_answers(respx_mock)
    mailbox.break_metadata("a1")

    assert run_brief(tmp_path / "brief.sqlite3") == 4

    output = capsys.readouterr().out
    assert route.call_count == 0
    assert "Brief: saved (empty); items: 0" in output
    assert "sync complete: no" in output
    assert "AI: nothing sent this run" in output
    assert "Inbox sync was incomplete, so this brief may be missing messages." in output


def test_show_prints_the_items_but_never_evidence(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    today = datetime.now(UTC).date().isoformat()

    def answer_with_action(request: httpx.Request) -> httpx.Response:
        results = [
            wire_result(
                sent["message_key"],
                sent["body"][:40],
                category="action",
                action_required=True,
                action_text="Approve the quarterly budget.",
                deadline_text="by Friday",
                deadline_date=today,
                deadline_time="17:00",
            )
            for sent in sent_messages(request)
        ]
        return httpx.Response(200, json=results_body(results))

    openai_answers(respx_mock, answer_with_action)
    replies(monkeypatch, "yes")

    assert run_brief(tmp_path / "brief.sqlite3", "--show") == 0

    output = capsys.readouterr().out
    assert "1. [actions] sender@example.com: Approval needed" in output
    assert "   A short summary." in output
    assert "   Action: Approve the quarterly budget." in output
    assert f"   Deadline: {today} 17:00 (UTC)" in output
    assert "   https://mail.google.com/" in output
    assert EVIDENCE not in output
    assert MARKER not in output


def test_ai_key_commands_never_print_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = MemoryVault()
    monkeypatch.setattr("mailbrief.providers.openai.credentials.os_vault", lambda: backend)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": f"  {TEST_KEY}  ")

    assert gmail.main(["ai-key", "status"]) == 0
    assert gmail.main(["ai-key", "set"]) == 0
    assert backend.entries == {(SERVICE, ENTRY): TEST_KEY}
    assert gmail.main(["ai-key", "status"]) == 0
    assert gmail.main(["ai-key", "clear"]) == 0
    assert gmail.main(["ai-key", "clear"]) == 0
    assert gmail.main(["ai-key", "status"]) == 0

    output = capsys.readouterr().out
    assert output.count("OpenAI API key: not saved") == 2
    assert "OpenAI API key: saved" in output
    assert "OpenAI API key saved in the OS credential store." in output
    assert "sk-" not in output
    assert "a" * 16 not in output


def test_an_invalid_key_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = MemoryVault()
    monkeypatch.setattr("mailbrief.providers.openai.credentials.os_vault", lambda: backend)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-too short")

    assert gmail.main(["ai-key", "set"]) == 3

    output = capsys.readouterr().out
    assert "That does not look like an OpenAI API key. Nothing was saved." in output
    assert "sk-too" not in output
    assert backend.entries == {}


def test_a_missing_key_or_model_is_a_setup_error(
    tmp_path: Path,
    mailbox: Mailbox,
    vault: MemoryVault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault.entries.clear()
    assert run_brief(tmp_path / "brief.sqlite3") == 3
    assert "No OpenAI API key is saved. Run: mailbrief-gmail-diagnostic ai-key set" in (
        capsys.readouterr().out
    )
    monkeypatch.delenv("MAILBRIEF_OPENAI_MODEL")

    assert run_brief(tmp_path / "brief.sqlite3") == 3
    assert "Set MAILBRIEF_OPENAI_MODEL" in capsys.readouterr().out


def test_consent_status_on_an_empty_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gmail.main(["ai-consent", "status", "--database", str(tmp_path / "empty.db")]) == 0
    assert "No accounts in this database." in capsys.readouterr().out


def test_the_body_outside_the_evidence_never_reaches_disk_output_or_logs(
    tmp_path: Path,
    mailbox: Mailbox,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    openai_answers(respx_mock)
    replies(monkeypatch, "yes")

    assert run_brief(tmp_path / "leak.sqlite3", "--show") == 0

    output = capsys.readouterr().out
    stored = b"".join(item.read_bytes() for item in sorted(tmp_path.iterdir()) if item.is_file())
    assert EVIDENCE.encode() in stored  # The quoted evidence is kept...
    assert MARKER.encode() not in stored  # ...and nothing else from the body.
    assert MARKER not in output
    assert MARKER not in caplog.text
