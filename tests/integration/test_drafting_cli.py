"""mailbrief-gmail-diagnostic drafts generate and ai-consent for drafting, end to end.

Gmail and Groq run over respx: Gmail through the brief tests' synthetic mailbox, so every
Gmail request can be checked to be a read-only GET.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from mailbrief.diagnostics import gmail
from mailbrief.providers.groq.provider import DRAFT_INSTRUCTIONS
from mailbrief.services.drafts import DraftService
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository
from tests.integration.test_brief_cli import (  # noqa: F401 - fixtures used by name
    MARKER,
    Mailbox,
    mailbox,
    midday,
    replies,
    run_brief,
    vault,
)
from tests.unit.providers.groq.groq_fixtures import (
    CHAT_URL,
    answer_every_message,
    error_body,
    output_text,
    response_body,
)

DRAFT = {"subject": None, "body": "Yes, approved. [[amount]]", "missing_context": ["the amount"]}


def groq(request: httpx.Request) -> httpx.Response:
    """Answer drafting calls with a draft and brief calls with analyses."""
    body = json.loads(request.content)
    if body["messages"][0]["content"] == DRAFT_INSTRUCTIONS:
        return httpx.Response(200, json=response_body([output_text(json.dumps(DRAFT))]))
    return answer_every_message(request)


def drafting_calls(route: respx.Route) -> list[httpx.Request]:
    return [
        call.request
        for call in route.calls
        if json.loads(call.request.content)["messages"][0]["content"] == DRAFT_INSTRUCTIONS
    ]


def create_reply(path: Path) -> str:
    async def create() -> str:
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                (account,) = await AccountRepository(session).list_all()
                draft = await DraftService(session).create_reply(account.email_address, "a1")
                return draft.public_id
        finally:
            await database.dispose()

    return asyncio.run(create())


def stored_bytes(path: Path) -> bytes:
    return b"".join(
        item.read_bytes() for item in path.parent.iterdir() if item.name.startswith(path.name)
    )


@pytest.fixture
def seeded(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, str, respx.Route]:
    """A saved brief (whose consent was typed) and a reply to its one email."""
    route = respx_mock.post(CHAT_URL).mock(side_effect=groq)
    replies(monkeypatch, "yes")
    path = tmp_path / "drafting.sqlite3"
    assert run_brief(path) == 0
    capsys.readouterr()
    return path, create_reply(path), route


def generate(path: Path, public_id: str, *options: str) -> int:
    return gmail.main(
        ["drafts", "generate", public_id, "--silent-only", "--database", str(path), *options]
    )


def test_first_use_asks_then_writes_a_version_from_the_email(
    seeded: tuple[Path, str, respx.Route],
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, public_id, route = seeded
    prompts = replies(monkeypatch, "yes")

    code = generate(
        path, public_id, "--use-email", "--use-text", "--tone", "warm", "--instructions", "Approve."
    )

    output = capsys.readouterr().out
    assert code == 0
    assert prompts == ['Type "yes" to consent and send: ']
    assert "MailBrief will send these parts to Groq (test-model) to write this draft:" in output
    assert "- The email: subject, sender name, received time and body:" in output
    assert "- Your current text: title and body:" in output
    assert "New version v2 from Groq; your previous text is version v1." in output
    assert "Placeholders: [[amount]]" in output
    assert "Missing context: the amount" in output
    assert f"drafts export {public_id}" in output
    (sent,) = drafting_calls(route)
    payload = json.loads(json.loads(sent.content)["messages"][1]["content"])
    assert set(payload) == {"kind", "tone", "length", "instructions", "today", "source", "current"}
    assert MARKER in payload["source"]["body"]
    assert "@" not in sent.content.decode()
    gmail_methods = {
        call.request.method
        for call in respx_mock.calls
        if call.request.url.host == "gmail.googleapis.com"
    }
    assert gmail_methods == {"GET"}  # Read-only: nothing is written to the mailbox.
    assert MARKER.encode() not in stored_bytes(path)

    assert gmail.main(["drafts", "export", public_id, "--database", str(path)]) == 0
    assert "Yes, approved. [[amount]]" in capsys.readouterr().out


def test_yes_skips_the_question_only_once_consent_is_recorded(
    seeded: tuple[Path, str, respx.Route],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, public_id, route = seeded

    assert generate(path, public_id, "--use-text", "--yes") == 6
    assert "First use needs your interactive consent" in capsys.readouterr().out
    assert drafting_calls(route) == []

    replies(monkeypatch, "yes")
    assert generate(path, public_id, "--use-text") == 0
    replies(monkeypatch)  # Any further prompt would fail the test.
    assert generate(path, public_id, "--use-text", "--yes") == 0
    replies(monkeypatch, "n")
    assert generate(path, public_id, "--use-text") == 6
    assert "Nothing was sent. The draft is unchanged." in capsys.readouterr().out
    assert len(drafting_calls(route)) == 2


def test_consent_status_and_revoke_cover_drafting(
    seeded: tuple[Path, str, respx.Route],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, public_id, _ = seeded
    assert gmail.main(["ai-consent", "status", "--database", str(path)]) == 0
    assert "AI drafting: Groq consent not granted" in capsys.readouterr().out
    replies(monkeypatch, "yes")
    assert generate(path, public_id, "--use-text") == 0
    capsys.readouterr()

    assert gmail.main(["ai-consent", "status", "--database", str(path)]) == 0
    assert "AI drafting: Groq consent granted" in capsys.readouterr().out
    assert gmail.main(["ai-consent", "revoke", "--database", str(path)]) == 0
    assert "Groq consent revoked: 2 (briefs 1, drafting 1)" in capsys.readouterr().out

    assert generate(path, public_id, "--use-text", "--yes") == 6  # It asks again.


def test_a_groq_failure_leaves_the_draft_and_exits_4(
    seeded: tuple[Path, str, respx.Route],
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, public_id, _ = seeded
    respx_mock.post(CHAT_URL).respond(401, json=error_body("invalid_api_key"))
    replies(monkeypatch, "yes")

    assert generate(path, public_id, "--use-text") == 4

    lines = capsys.readouterr().out.splitlines()
    assert lines[-3:] == [
        "Groq rejected the API key. Run: mailbrief-gmail-diagnostic ai-key set",
        "Groq detail: HTTP 401, code invalid_api_key",
        "The draft is unchanged.",
    ]


def test_unavailable_parts_and_unknown_drafts_exit_3(
    seeded: tuple[Path, str, respx.Route], capsys: pytest.CaptureFixture[str]
) -> None:
    path, public_id, route = seeded

    assert generate(path, public_id, "--use-action") == 3
    assert capsys.readouterr().out == "That context is no longer available; choose again.\n"
    assert generate(path, "00000000-0000-4000-8000-000000000999", "--use-text") == 3
    assert capsys.readouterr().out == "That draft was not found.\n"
    assert drafting_calls(route) == []


def test_generate_help_lists_its_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        gmail.main(["drafts", "generate", "--help"])

    assert exited.value.code == 0
    shown = capsys.readouterr().out
    for option in (
        "--use-email",
        "--use-action",
        "--use-text",
        "--tone",
        "--length",
        "--instructions",
        "--yes",
        "--silent-only",
        "--database",
    ):
        assert option in shown
