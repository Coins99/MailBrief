"""mailbrief-gmail-diagnostic preferences show, and saved preferences in the daily commands.

Gmail and Groq run over respx through the brief tests' synthetic mailbox, whose one
message (a1) comes from sender@example.com.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx

from mailbrief.config import Settings
from mailbrief.diagnostics import gmail
from mailbrief.domain.drafts import DraftTone
from mailbrief.domain.preferences import AI_LIMIT_FIELDS, PreferencesEdit
from mailbrief.ports.errors import AuthenticationRequiredError
from mailbrief.services.preferences import PreferencesService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from tests.integration.test_brief_cli import (  # noqa: F401 - fixtures used by name
    Mailbox,
    groq_answers,
    mailbox,
    midday,
    replies,
    vault,
)

UNREADABLE = "Saved preferences could not be read. Open Settings > Preferences and reset them."


@pytest.fixture(autouse=True)
def toronto_system_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("America/Toronto"))
    for name in AI_LIMIT_FIELDS:
        monkeypatch.delenv(f"MAILBRIEF_{name.upper()}", raising=False)


def save_preferences(path: Path, edit: PreferencesEdit) -> None:
    async def save() -> None:
        await asyncio.to_thread(upgrade_database, path)
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                service = PreferencesService(session)
                await service.save(edit, (await service.get()).revision)
        finally:
            await database.dispose()

    asyncio.run(save())


def corrupt(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE owner_preferences SET excluded_senders_json = 'not json'")
        connection.commit()


def show(path: Path) -> int:
    return gmail.main(["preferences", "show", "--database", str(path)])


def test_show_on_a_fresh_database_prints_the_defaults_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fresh.sqlite3"

    assert show(path) == 0

    assert capsys.readouterr().out.splitlines() == [
        "No database yet: these are the defaults.",
        "Time zone: system (America/Toronto)",
        "Messages per brief: 10",
        "Excluded senders: none",
        "Drafting defaults: tone neutral, length medium",
        "Refresh when MailBrief starts: no",
        "Refresh while running: off",
        "Automatic analysis: unavailable until you give consent "
        "(analyze once with Sync and review)",
        "AI limits (a MAILBRIEF_AI_* variable wins over a saved value):",
        "  Messages per AI request: 1 (default)",
        "  Body characters sent: 4000 (default)",
        "  Output tokens: 4000 (default)",
        "  Requests per run: 10 (default)",
        "  Timeout (seconds): 120 (default)",
    ]
    assert not path.exists()


def test_show_lists_rules_and_where_each_limit_comes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "saved.sqlite3"
    save_preferences(
        path,
        PreferencesEdit(
            time_zone="Asia/Tokyo",
            shortlist_limit=3,
            excluded_senders=("@news.example.com", "Boss@Example.com"),
            draft_tone=DraftTone.WARM,
            ai_batch_size=2,
            ai_body_character_limit=2_000,
            ai_timeout_seconds=90.5,
        ),
    )
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "4")

    assert show(path) == 0

    output = capsys.readouterr().out
    assert "Time zone: Asia/Tokyo\n" in output
    assert "Messages per brief: 3\n" in output
    assert "Excluded senders: 2\n  @news.example.com\n  boss@example.com\n" in output
    assert "Drafting defaults: tone warm, length medium\n" in output
    assert "  Messages per AI request: 4 (environment)\n" in output
    assert "  Body characters sent: 2000 (saved)\n" in output
    assert "  Output tokens: 4000 (default)\n" in output
    assert "  Timeout (seconds): 90.5 (saved)\n" in output


def test_unreadable_preferences_exit_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "corrupt.sqlite3"
    save_preferences(path, PreferencesEdit(excluded_senders=("@example.com",)))
    corrupt(path)

    assert show(path) == 3
    assert capsys.readouterr().out.strip() == UNREADABLE


def test_a_database_that_can_t_be_opened_exits_5(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert show(tmp_path) == 5  # A directory, not a database.
    assert "Local database or file operation failed" in capsys.readouterr().out


def test_sync_uses_the_saved_zone_unless_timezone_is_given(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "zone.sqlite3"
    # At 12:00 UTC on the 16th it is already the 17th in Kiritimati (UTC+14).
    save_preferences(path, PreferencesEdit(time_zone="Pacific/Kiritimati"))

    assert gmail.main(["sync", "--silent-only", "--database", str(path)]) == 0
    assert "Inbox date: 2026-09-17 (Pacific/Kiritimati)" in capsys.readouterr().out

    arguments = ["sync", "--silent-only", "--database", str(path), "--timezone", "UTC"]
    assert gmail.main(arguments) == 0
    assert "Inbox date: 2026-09-16 (UTC)" in capsys.readouterr().out

    blank = ["sync", "--silent-only", "--database", str(path), "--timezone", " "]
    assert gmail.main(blank) == 0  # A blank flag is no flag.
    assert "Inbox date: 2026-09-17 (Pacific/Kiritimati)" in capsys.readouterr().out


def test_sync_labels_excluded_messages(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "labels.sqlite3"
    save_preferences(path, PreferencesEdit(excluded_senders=("@example.com",)))
    mailbox.add("a2")

    arguments = ["sync", "--silent-only", "--database", str(path), "--timezone", "UTC"]
    assert gmail.main([*arguments, "--show-metadata"]) == 0

    output = capsys.readouterr().out
    assert "selected: 0" in output
    assert "a1 [excluded]" in output and "a2 [excluded]" in output


def full_body_requests(router: respx.MockRouter) -> int:
    return sum(call.request.url.params.get("format") == "full" for call in router.calls)


def test_an_excluded_include_exits_3_before_any_body_or_ai(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "include.sqlite3"
    save_preferences(path, PreferencesEdit(excluded_senders=("sender@example.com",)))
    route = groq_answers(respx_mock)
    base = ["--silent-only", "--database", str(path), "--timezone", "UTC", "--include", "a1"]

    for command in ("brief", "bodies"):
        assert gmail.main([command, *base]) == 3
        output = capsys.readouterr().out
        assert "A message from an excluded sender can't be included" in output
        assert "sender@example.com" not in output

    assert route.call_count == 0
    assert full_body_requests(respx_mock) == 0


def test_a_brief_never_reads_or_sends_an_excluded_sender(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "brief.sqlite3"
    save_preferences(path, PreferencesEdit(excluded_senders=("@example.com",)))
    route = groq_answers(respx_mock)
    replies(monkeypatch)  # Nothing to consent to: no prompt may appear.

    code = gmail.main(["brief", "--silent-only", "--database", str(path), "--timezone", "UTC"])

    assert code == 0
    assert "Coverage: shortlisted 0" in capsys.readouterr().out
    assert route.call_count == 0
    assert full_body_requests(respx_mock) == 0


def test_unreadable_preferences_stop_a_brief_before_gmail_or_groq(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "closed.sqlite3"
    save_preferences(path, PreferencesEdit(excluded_senders=("@example.com",)))
    corrupt(path)
    route = groq_answers(respx_mock)

    code = gmail.main(["brief", "--silent-only", "--database", str(path), "--timezone", "UTC"])

    assert code == 3
    assert capsys.readouterr().out.strip() == UNREADABLE
    assert route.call_count == 0
    assert not respx_mock.calls  # Gmail was never contacted either.


def test_listing_actions_only_displays_so_it_falls_back_to_the_system_zone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "actions.sqlite3"
    save_preferences(path, PreferencesEdit(time_zone="Asia/Tokyo"))
    corrupt(path)

    assert gmail.main(["actions", "list", "--database", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Saved preferences could not be read; showing the system time zone.",
        "No actions.",
    ]


@pytest.mark.parametrize("command", ["sync", "bodies", "brief"])
def test_a_missing_database_is_not_created_before_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vault: object,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    class SignedOut:
        async def connect(self) -> None:
            raise AuthenticationRequiredError("Reconnect Gmail.")

    @asynccontextmanager
    async def factory(settings: Settings, *, silent_only: bool = False) -> AsyncIterator[object]:
        yield SignedOut()

    monkeypatch.setattr(gmail, "gmail_provider", factory)
    monkeypatch.setenv("MAILBRIEF_GROQ_MODEL", "test-model")
    path = tmp_path / "missing" / "mailbrief.sqlite3"

    assert gmail.main([command, "--silent-only", "--database", str(path)]) == 2
    assert capsys.readouterr().out.strip() == "Reconnect Gmail."
    assert not path.parent.exists()
