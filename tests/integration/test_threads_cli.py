"""Thread tracking from the CLI: sync and brief check threads, actions list shows the
activity, and actions seen clears it. Gmail and Groq run over respx."""

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from mailbrief.diagnostics import gmail
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.database import Database
from mailbrief.storage.tables import AccountTable, ActionTable, MessageTable
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
    # Archived at once: it is still tracked, but never joins today's shortlist.
    mailbox.reply("r1", "a1", received=MIDDAY + timedelta(hours=1), labels=())
    # The owner's reply today is cached as sent and never reaches the brief either.
    mailbox.reply("mine", "a1", received=MIDDAY + timedelta(hours=2), labels=("SENT",))
    groq_answers(respx_mock)
    replies(monkeypatch, "yes")
    capsys.readouterr()

    assert run_brief(path) == 0

    output = capsys.readouterr().out
    assert "Tracked threads: 1 checked, 0 failed, 2 messages saved" in output
    assert "Coverage: shortlisted 1," in output  # Only a1: both replies stay out.
    assert mailbox.thread_route.call_count == 1


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
