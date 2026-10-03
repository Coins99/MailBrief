"""brief --date, briefs list and briefs show, end to end.

Gmail and Groq run over respx through the brief tests' synthetic mailbox; briefs list and
briefs show are offline, so any provider use fails the test.
"""

import asyncio
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import respx
import time_machine

from mailbrief.diagnostics import gmail
from mailbrief.domain.digests import DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import AccountRepository, DigestRepository
from tests.integration.test_brief_cli import (  # noqa: F401 - fixtures used by name
    MIDDAY,
    Mailbox,
    groq_answers,
    mailbox,
    midday,
    replies,
    vault,
)

TODAY = MIDDAY.date()  # 2026-09-16 in UTC.
YESTERDAY = TODAY - timedelta(days=1)
OUT_OF_RANGE = "Choose today or one of the previous 7 days."


@pytest.fixture(autouse=True)
def utc_system_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("UTC"))


def run_brief(path: Path, *options: str) -> int:
    return gmail.main(["brief", "--silent-only", "--database", str(path), *options])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("yesterday", "Write the date as YYYY-MM-DD, such as 2026-09-29."),
        ("2026-09-08", OUT_OF_RANGE),  # Eight days back.
        ("2026-09-17", OUT_OF_RANGE),  # Tomorrow.
    ],
)
def test_a_date_that_can_t_be_briefed_exits_3_before_gmail(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
    text: str,
    message: str,
) -> None:
    path = tmp_path / "bounds.sqlite3"

    assert run_brief(path, "--date", text) == 3

    assert capsys.readouterr().out.strip() == message
    assert not respx_mock.calls
    assert not path.exists()


def test_a_past_day_is_briefed_and_shown_with_what_it_covers(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with time_machine.travel(MIDDAY - timedelta(days=1), tick=False):
        mailbox.add("y1")  # Received yesterday; a1 arrived today.
    route = groq_answers(respx_mock)
    replies(monkeypatch, "yes")
    path = tmp_path / "past.sqlite3"

    assert run_brief(path, "--date", YESTERDAY.isoformat(), "--show") == 0

    output = capsys.readouterr().out
    assert f"Inbox date: {YESTERDAY} (UTC)" in output
    assert "Brief: saved (complete); items: 1" in output
    assert (
        f"Covers messages received on {YESTERDAY} (UTC) that were still in your Inbox on "
        f"{TODAY} at 12:00."
    ) in output
    assert route.call_count == 1

    assert gmail.main(["briefs", "show", YESTERDAY.isoformat(), "--database", str(path)]) == 0
    shown = capsys.readouterr().out
    assert f"Brief for {YESTERDAY} (me@example.com): complete; items: 1" in shown
    assert "still in your Inbox" in shown


async def seed(path: Path) -> None:
    """Two Gmail accounts and an Outlook one, with briefs on a few days."""
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.transaction() as session:
            ids = {}
            for email, provider in (
                ("owner@example.com", ProviderKind.GMAIL),
                ("other@example.com", ProviderKind.GMAIL),
                ("work@example.com", ProviderKind.MICROSOFT),
            ):
                account = await AccountRepository(session).upsert(
                    AccountIdentity(
                        provider=provider, provider_account_id=email, email_address=email
                    )
                )
                ids[email] = account.id
            for email, day in (
                ("owner@example.com", TODAY),
                ("owner@example.com", TODAY - timedelta(days=2)),
                ("other@example.com", TODAY - timedelta(days=2)),
                ("work@example.com", YESTERDAY),
            ):
                await DigestRepository(session).save_digest(
                    account_id=ids[email],
                    local_date=day,
                    timezone_name="UTC",
                    status=DigestStatus.EMPTY,
                )
    finally:
        await database.dispose()


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("briefs commands are offline"))
    monkeypatch.setattr(gmail, "gmail_provider", forbidden)
    monkeypatch.setattr(gmail, "groq_provider", forbidden)


def test_briefs_list_shows_saved_briefs_then_missed_days(
    tmp_path: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "list.sqlite3"
    asyncio.run(seed(path))

    assert gmail.main(["briefs", "list", "--database", str(path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{TODAY} owner@example.com: empty; 0 items"
    assert lines[1].startswith(f"  Covers messages received on {TODAY} up to ")
    assert [line.split(":")[0] for line in lines[2::2][:2]] == [
        f"{TODAY - timedelta(days=2)} other@example.com",
        f"{TODAY - timedelta(days=2)} owner@example.com",
    ]
    missed = [line for line in lines if line.startswith("Missed days")]
    every_day = [TODAY - timedelta(days=back) for back in range(1, 8)]
    assert missed == [
        "Missed days for owner@example.com (last 7 days): "
        + ", ".join(day.isoformat() for day in every_day if day != TODAY - timedelta(days=2)),
        "Missed days for other@example.com (last 7 days): "
        + ", ".join(day.isoformat() for day in every_day if day != TODAY - timedelta(days=2)),
    ]
    assert "work@example.com" not in "\n".join(lines)

    assert gmail.main(["briefs", "list", "--database", str(path), "--limit", "1"]) == 0
    assert len([line for line in capsys.readouterr().out.splitlines() if "items" in line]) == 1


def test_briefs_list_on_a_missing_database_creates_nothing(
    tmp_path: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing.sqlite3"
    assert gmail.main(["briefs", "list", "--database", str(path)]) == 0
    assert capsys.readouterr().out == "No saved briefs.\n"
    assert not path.exists()


def test_briefs_show_found_missing_and_ambiguous(
    tmp_path: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "show.sqlite3"
    asyncio.run(seed(path))
    base = ["briefs", "show", "--database", str(path)]
    two_back = (TODAY - timedelta(days=2)).isoformat()

    assert gmail.main([*base, TODAY.isoformat()]) == 0
    assert capsys.readouterr().out.startswith(
        f"Brief for {TODAY} (owner@example.com): empty; items: 0\nCovers messages received"
    )

    assert gmail.main([*base, two_back]) == 3
    assert capsys.readouterr().out == (
        "Several accounts have a brief for that date; pass --account.\n"
    )
    assert gmail.main([*base, two_back, "--account", "other@example.com"]) == 0
    assert "(other@example.com)" in capsys.readouterr().out

    for day in (YESTERDAY.isoformat(), "2020-01-01"):  # Outlook's, and none at all.
        assert gmail.main([*base, day]) == 3
        assert capsys.readouterr().out == "No saved brief for that date.\n"
    assert gmail.main([*base, "not-a-date"]) == 3
    assert "YYYY-MM-DD" in capsys.readouterr().out
    assert gmail.main(["briefs", "show", TODAY.isoformat(), "--database", str(tmp_path / "x")]) == 3
    assert not (tmp_path / "x").exists()


def test_the_timezone_flag_decides_today(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At 12:00 UTC on the 16th it's already the 17th in Kiritimati, so the 9th is 8 days
    back there but 7 days back in UTC."""
    path = tmp_path / "zone.sqlite3"
    ninth = date(2026, 9, 9).isoformat()

    code = run_brief(path, "--date", ninth, "--timezone", "Pacific/Kiritimati")

    assert code == 3
    assert capsys.readouterr().out.strip() == OUT_OF_RANGE
    assert not respx_mock.calls


def test_briefs_list_counts_missed_days_in_the_timezone_given(
    tmp_path: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """At 12:00 UTC on the 16th it's already the 17th in Kiritimati, so the last 7 days
    there end on the 10th rather than the 9th."""
    path = tmp_path / "zone.sqlite3"
    asyncio.run(seed(path))
    base = ["briefs", "list", "--database", str(path)]

    def owner_missed() -> str:
        return [line for line in capsys.readouterr().out.splitlines() if "owner@" in line][-1]

    assert gmail.main(base) == 0
    assert owner_missed().endswith("2026-09-10, 2026-09-09")  # The system zone: UTC.
    assert gmail.main([*base, "--timezone", "Pacific/Kiritimati"]) == 0
    assert owner_missed().endswith("2026-09-11, 2026-09-10")

    assert gmail.main([*base, "--timezone", "Mars/Base"]) == 3
    assert "Invalid timezone" in capsys.readouterr().out
