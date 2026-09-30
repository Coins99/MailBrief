"""Follow-up proposals from the CLI: brief proposes, actions proposals lists, and
apply-proposal and dismiss-proposal decide. Gmail and Groq run over respx."""

import asyncio
import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from mailbrief.diagnostics import gmail
from mailbrief.services.actions import ActionService
from mailbrief.storage.database import Database
from tests.integration.test_brief_cli import (  # noqa: F401 - fixtures used by name
    MIDDAY,
    Mailbox,
    groq_answers,
    mailbox,
    midday,
    replies,
    run_brief,
    vault,
)
from tests.integration.test_threads_cli import sync, track_a1
from tests.unit.providers.groq.groq_fixtures import results_body, sent_messages, wire_result

QUOTE = "quarterly budget by Friday"


def answer_with_a_new_deadline(request: httpx.Request) -> httpx.Response:
    """Every email moves the deadline to Friday, 18 September."""
    results = [
        wire_result(
            sent["message_key"],
            sent["body"][:40],
            deadline_text="by Friday",
            deadline_date="2026-09-18",
            follow_up="new_deadline",
            follow_up_evidence=QUOTE,
        )
        for sent in sent_messages(request)
    ]
    return httpx.Response(200, json=results_body(results))


def actions(path: Path, *options: str) -> int:
    return gmail.main(["actions", *options, "--database", str(path)])


def with_a_reply(path: Path, inbox: Mailbox, respx_mock: respx.MockRouter) -> str:
    """An open action from a1, and a reply in its thread that arrives later."""
    assert sync(path) == 0
    public_id = track_a1(path)
    inbox.add("r1", thread="thread_a1", received=MIDDAY + timedelta(minutes=30))
    groq_answers(respx_mock, answer_with_a_new_deadline)
    return public_id


def test_brief_proposes_and_apply_proposal_updates_the_action(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "proposals.sqlite3"
    public_id = with_a_reply(path, mailbox, respx_mock)
    replies(monkeypatch, "yes")
    capsys.readouterr()

    assert run_brief(path, "--show") == 0
    shown = capsys.readouterr().out
    # a1 is the action's own source, so only the reply proposes.
    assert "Proposed 1 update to your actions.\n" in shown
    assert re.search(
        r"^   Proposes: new deadline 2026-09-18 for Approve the budget \(P(\d+)\)$",
        shown,
        re.MULTILINE,
    )
    (number,) = re.findall(r"\(P(\d+)\)", shown)
    assert gmail.main(["briefs", "show", "2026-09-16", "--database", str(path)]) == 0
    assert f"for Approve the budget (P{number})" in capsys.readouterr().out

    assert actions(path, "proposals", "--timezone", "UTC") == 0
    assert capsys.readouterr().out == (
        f"P{number} · {public_id} Approve the budget · new deadline 2026-09-18 · "
        f'"{QUOTE}" · sender@example.com, 2026-09-16, Approval needed\n'
    )
    assert actions(path, "list", "--timezone", "UTC") == 0
    assert capsys.readouterr().out.rstrip().endswith("; 1 proposal")

    assert actions(path, "apply-proposal", f"P{number}", "--timezone", "UTC") == 0
    assert capsys.readouterr().out == (
        f"Applied P{number}: Approve the budget ({public_id}) now has deadline 2026-09-18; "
        "target date 2026-09-17.\n"
    )
    assert actions(path, "list", "--timezone", "UTC") == 0
    listed = capsys.readouterr().out
    assert "; target 2026-09-17; deadline 2026-09-18;" in listed
    assert "proposal" not in listed

    decided = "That proposal was already applied or dismissed.\n"
    for command in ("apply-proposal", "dismiss-proposal"):
        assert actions(path, command, number) == 3
        assert capsys.readouterr().out == decided
    assert actions(path, "apply-proposal", "P999") == 3
    assert capsys.readouterr().out == "That proposal was not found.\n"
    assert actions(path, "proposals") == 0
    assert capsys.readouterr().out == "No pending proposals.\n"


def test_a_dismissed_proposal_does_not_come_back(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "dismissed.sqlite3"
    with_a_reply(path, mailbox, respx_mock)
    replies(monkeypatch, "yes")
    assert run_brief(path) == 0
    (number,) = re.findall(r"Proposed (\d+) update", capsys.readouterr().out)
    assert number == "1"
    assert actions(path, "proposals") == 0
    (proposal,) = re.findall(r"^P(\d+) ", capsys.readouterr().out, re.MULTILINE)

    assert actions(path, "dismiss-proposal", f"p{proposal}") == 0
    assert capsys.readouterr().out == f"Dismissed P{proposal}; it won't be proposed again.\n"
    assert run_brief(path, "--yes") == 0

    assert "Proposed" not in capsys.readouterr().out
    assert actions(path, "proposals") == 0
    assert capsys.readouterr().out == "No pending proposals.\n"


def test_a_proposal_for_a_completed_action_is_refused(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "completed.sqlite3"
    public_id = with_a_reply(path, mailbox, respx_mock)
    replies(monkeypatch, "yes")
    assert run_brief(path) == 0
    assert actions(path, "proposals") == 0
    (proposal,) = re.findall(r"^P(\d+) ", capsys.readouterr().out, re.MULTILINE)

    async def complete() -> None:
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                await ActionService(session).complete(public_id, 1)
        finally:
            await database.dispose()

    asyncio.run(complete())

    assert actions(path, "apply-proposal", proposal) == 3
    assert capsys.readouterr().out == (
        "The action is no longer open, so the proposal can't be applied.\n"
    )


def test_sync_shows_why_a_reply_in_a_tracked_thread_ranked_higher(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "ranked.sqlite3"
    assert sync(path) == 0
    track_a1(path)
    mailbox.add("r1", thread="thread_a1", received=MIDDAY + timedelta(minutes=30))
    capsys.readouterr()

    assert (
        gmail.main(
            [
                "sync",
                "--silent-only",
                "--database",
                str(path),
                "--timezone",
                "UTC",
                "--show-metadata",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    reasons = re.findall(r"^  reasons: (.*)$", output, re.MULTILINE)
    assert len(reasons) == 2
    assert any("reply in a thread you track" in line for line in reasons)
    assert sum("reply in a thread you track" in line for line in reasons) == 1


def test_proposal_ids_must_look_like_the_listing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        actions(tmp_path / "unused.sqlite3", "apply-proposal", "X3")

    assert caught.value.code == 2
    assert "such as P3" in capsys.readouterr().err
