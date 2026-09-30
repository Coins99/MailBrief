"""Thread tracking from the CLI: sync and brief check threads, actions list shows the
activity, actions seen clears it, briefs name the actions an email continues, and accept
--into adds the email to one. Gmail and Groq run over respx."""

import asyncio
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from mailbrief.diagnostics import gmail
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.database import Database
from mailbrief.storage.tables import AccountTable, ActionSourceTable, ActionTable, MessageTable
from tests.integration.test_brief_cli import (  # noqa: F401 - fixtures used by name
    MIDDAY,
    Mailbox,
    answer_with_two_actions,
    groq_answers,
    mailbox,
    midday,
    replies,
    run_brief,
    vault,
)


def sync(path: Path) -> int:
    return gmail.main(["sync", "--silent-only", "--database", str(path), "--timezone", "UTC"])


def track_a1(path: Path) -> str:
    """An open action whose source is the cached message a1."""

    async def create() -> str:
        database = Database.from_path(path)
        try:
            async with database.transaction() as session:
                message = await session.scalar(
                    select(MessageTable).where(MessageTable.provider_message_id == "a1")
                )
                assert message is not None
                account = await session.get(AccountTable, message.account_id)
                assert account is not None
                repository = ActionRepository(session)
                row = await repository.add_action(
                    ActionTable(
                        public_id=str(uuid.uuid4()),
                        title="Approve the budget",
                        ownership="mine",
                        status="open",
                        deadline_precision="none",
                        created_at_utc=MIDDAY,
                        updated_at_utc=MIDDAY,
                        revision=1,
                    )
                )
                await repository.add_source(row.id, message, account)
                return row.public_id
        finally:
            await database.dispose()

    return asyncio.run(create())


def actions_list(path: Path) -> list[str]:
    return [
        "list",
        "--database",
        str(path),
        "--timezone",
        "UTC",
    ]


def test_sync_checks_threads_and_actions_show_then_clear_the_activity(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "threads.sqlite3"
    assert sync(path) == 0
    public_id = track_a1(path)
    capsys.readouterr()
    mailbox.reply("r1", "a1", received=MIDDAY + timedelta(hours=1), labels=("INBOX",))
    mailbox.reply("mine", "a1", received=MIDDAY + timedelta(hours=2), labels=("SENT",))
    mailbox.reply("draft", "a1", received=MIDDAY + timedelta(hours=3), labels=("DRAFT",))

    assert sync(path) == 0
    assert "Tracked threads: 1 checked, 0 failed, 2 messages saved" in capsys.readouterr().out

    assert gmail.main(["actions", *actions_list(path)]) == 0
    listed = capsys.readouterr().out
    assert "; thread: 1 new, latest 2026-09-16 13:00 from Sender; you replied 2026-09-16" in listed

    assert gmail.main(["actions", "seen", public_id, "--database", str(path)]) == 0
    assert capsys.readouterr().out == "Marked seen.\n"
    assert gmail.main(["actions", "seen", public_id, "--database", str(path)]) == 0
    assert capsys.readouterr().out == "Nothing new in its threads.\n"
    assert gmail.main(["actions", *actions_list(path)]) == 0
    assert "thread:" not in capsys.readouterr().out


def test_seeing_an_unknown_action_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "unknown.sqlite3"
    code = gmail.main(
        ["actions", "seen", "00000000-0000-4000-8000-000000000000", "--database", str(path)]
    )
    assert code == 3
    assert "not found" in capsys.readouterr().out


def test_brief_reports_the_thread_check(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "brief.sqlite3"
    assert sync(path) == 0
    track_a1(path)
    # Archived at once, it isn't in today's Inbox, but it is a reply in a tracked thread, so
    # it joins the shortlist as an outside reply (ADR 0016).
    mailbox.reply("r1", "a1", received=MIDDAY + timedelta(hours=1), labels=())
    # The owner's reply today is cached as sent and never reaches the brief.
    mailbox.reply("mine", "a1", received=MIDDAY + timedelta(hours=2), labels=("SENT",))
    groq_answers(respx_mock)
    replies(monkeypatch, "yes")
    capsys.readouterr()

    assert run_brief(path) == 0

    output = capsys.readouterr().out
    assert "Tracked threads: 1 checked, 0 failed, 2 messages saved" in output
    assert "Coverage: shortlisted 2," in output  # a1 and the archived reply r1, not "mine".
    assert mailbox.thread_route.call_count == 1


def sync_with_metadata(path: Path) -> int:
    return gmail.main(
        ["sync", "--silent-only", "--database", str(path), "--timezone", "UTC", "--show-metadata"]
    )


def test_sync_show_metadata_labels_replies_outside_the_inbox(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "outside.sqlite3"
    assert sync(path) == 0
    track_a1(path)
    # Archived at once, so today's Inbox never lists it; the owner's own reply is never shown.
    mailbox.reply("r1", "a1", received=MIDDAY + timedelta(hours=1), labels=())
    mailbox.reply("mine", "a1", received=MIDDAY + timedelta(hours=2), labels=("SENT",))
    capsys.readouterr()

    assert sync_with_metadata(path) == 0

    lines = capsys.readouterr().out.splitlines()
    (reply,) = (index for index, line in enumerate(lines) if line.startswith("r1 [selected]"))
    assert lines[reply + 1] == "  reply in a tracked thread, not in today's Inbox"
    assert not any("mine" in line for line in lines)
    # Today's own Inbox message carries no such label.
    (inbox,) = (index for index, line in enumerate(lines) if line.startswith("a1 ["))
    assert "not in today's Inbox" not in " ".join(lines[inbox : inbox + 3])


def test_brief_show_puts_outside_replies_in_their_own_section(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "section.sqlite3"
    assert sync(path) == 0
    track_a1(path)
    mailbox.reply("r1", "a1", received=MIDDAY + timedelta(hours=1), labels=())
    groq_answers(respx_mock)
    replies(monkeypatch, "yes")
    capsys.readouterr()

    assert run_brief(path, "--show") == 0

    shown = capsys.readouterr().out
    assert re.search(r"^2\. \[follow_ups\] ", shown, re.MULTILINE)  # After a1, which is first.
    assert "Also includes 1 reply from a thread you track that wasn't in today's Inbox." in shown
    assert gmail.main(["briefs", "show", "2026-09-16", "--database", str(path)]) == 0
    assert "[follow_ups]" in capsys.readouterr().out


def test_a_rate_limited_check_is_reported_and_the_sync_still_succeeds(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "limited.sqlite3"
    assert sync(path) == 0
    track_a1(path)
    mailbox.thread_route.respond(429, headers={"Retry-After": "600"})
    capsys.readouterr()

    assert sync(path) == 0

    assert (
        "Tracked threads: 0 checked, 0 failed, 0 messages saved; stopped: RATE_LIMITED"
        in capsys.readouterr().out
    )


def test_no_open_actions_means_no_thread_line(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sync(tmp_path / "quiet.sqlite3") == 0
    assert "Tracked threads" not in capsys.readouterr().out
    assert mailbox.thread_route.call_count == 0


def track_earlier(path: Path, title: str) -> str:
    """An open action whose one source is an earlier message in a1's thread, so a1
    continues it."""

    async def create() -> str:
        database = Database.from_path(path)
        try:
            async with database.transaction() as session:
                message = await session.scalar(
                    select(MessageTable).where(MessageTable.provider_message_id == "a1")
                )
                assert message is not None
                account = await session.get(AccountTable, message.account_id)
                assert account is not None
                row = await ActionRepository(session).add_action(
                    ActionTable(
                        public_id=str(uuid.uuid4()),
                        title=title,
                        ownership="waiting_for",
                        status="open",
                        deadline_precision="none",
                        created_at_utc=MIDDAY - timedelta(days=1),
                        updated_at_utc=MIDDAY - timedelta(days=1),
                        revision=1,
                    )
                )
                session.add(
                    ActionSourceTable(
                        action_id=row.id,
                        provider_message_id="a0",
                        subject="Budget",
                        sender_address="sender@example.com",
                        web_link=message.web_link,
                        received_at_utc=MIDDAY - timedelta(days=1),
                        provider=account.provider,
                        provider_account_id=account.provider_account_id,
                        provider_thread_id=message.conversation_id,
                    )
                )
                return row.public_id
        finally:
            await database.dispose()

    return asyncio.run(create())


def test_briefs_show_continuations_and_accept_into_adds_to_the_action(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "continues.sqlite3"
    assert sync(path) == 0
    public_id = track_earlier(path, "\x1b[2JChase the budget")
    groq_answers(respx_mock, answer_with_two_actions)
    replies(monkeypatch, "yes")
    capsys.readouterr()

    assert run_brief(path, "--show") == 0
    shown = capsys.readouterr().out
    continues = f"   Continues: [2JChase the budget ({public_id})\n"
    assert continues in shown and "\x1b" not in shown
    mine, waiting = re.findall(r"\[pending #(\d+)\]", shown)
    assert gmail.main(["briefs", "show", "2026-09-16", "--database", str(path)]) == 0
    assert continues in capsys.readouterr().out

    def actions(*options: str) -> int:
        return gmail.main(["actions", *options, "--database", str(path)])

    assert actions("accept", waiting, "--into", public_id) == 0
    assert capsys.readouterr().out == f"Added to: [2JChase the budget ({public_id})\n"
    assert actions("list", "--view", "waiting", "--timezone", "UTC") == 0
    assert capsys.readouterr().out.count("Chase the budget") == 1  # No second action.

    # a1 is now one of the action's sources, so it no longer "continues" it.
    assert gmail.main(["briefs", "show", "2026-09-16", "--database", str(path)]) == 0
    after = capsys.readouterr().out
    assert "Continues:" not in after
    assert "   [accepted] Wait for the signed copy\n" in after

    assert actions("accept", mine) == 0
    capsys.readouterr()
    assert actions("accept", mine, "--into", public_id) == 3
    assert capsys.readouterr().out == "That suggestion already belongs to another action.\n"
    refused = "That suggestion or action was not found or cannot change now.\n"
    assert actions("accept", mine, "--into", "00000000-0000-4000-8000-000000000000") == 3
    assert capsys.readouterr().out == refused
    assert actions("accept", "999999", "--into", public_id) == 3
    assert capsys.readouterr().out == refused
