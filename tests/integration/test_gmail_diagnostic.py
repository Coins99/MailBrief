"""Real migration/database/CLI composition with synthetic Gmail metadata."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from alembic.util import CommandError

from mailbrief.config import Settings
from mailbrief.diagnostics import gmail
from mailbrief.providers.gmail.client import MESSAGES_URL, GmailClient
from mailbrief.providers.gmail.provider import GmailProvider
from tests.unit.providers.gmail.body_fixtures import message, part
from tests.unit.providers.gmail.metadata_fixtures import FakeSession, metadata


@pytest.mark.parametrize("show_metadata", [False, True])
def test_sync_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    respx_mock: respx.MockRouter,
    show_metadata: bool,
) -> None:
    @asynccontextmanager
    async def factory(
        settings: Settings, *, silent_only: bool = False
    ) -> AsyncIterator[GmailProvider]:
        async with httpx.AsyncClient() as http:
            yield GmailProvider(
                FakeSession(), GmailClient(http, FakeSession()), silent_only=silent_only
            )

    monkeypatch.setattr(gmail, "gmail_provider", factory)
    monkeypatch.setenv("MAILBRIEF_DATABASE_URL", "sqlite+aiosqlite:///must-not-be-used.db")
    respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "a1"}]})
    respx_mock.get(MESSAGES_URL + "/a1").respond(json=metadata(received=datetime.now(UTC)))
    path = tmp_path / "diagnostic.sqlite3"
    args = ["sync", "--silent-only", "--database", str(path), "--timezone", "UTC"]
    if show_metadata:
        args.append("--show-metadata")
    assert gmail.main(args) == 0
    assert path.is_file()
    output = capsys.readouterr().out
    assert "Status: complete" in output and "retrieved: 1" in output and "selected: 1" in output
    assert ("sender@example.com" in output) is show_metadata
    assert ("Approval needed" in output) is show_metadata
    assert gmail.main([*args, "--exclude", "a1"]) == 0
    assert "selected: 0" in capsys.readouterr().out


def test_bad_timezone_does_not_connect(capsys: pytest.CaptureFixture[str]) -> None:
    assert gmail.main(["sync", "--timezone", "not/a/timezone"]) == 3
    assert "Invalid timezone" in capsys.readouterr().out


def test_alembic_failure_reports_database_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @asynccontextmanager
    async def factory(
        settings: Settings, *, silent_only: bool = False
    ) -> AsyncIterator[GmailProvider]:
        async with httpx.AsyncClient() as http:
            yield GmailProvider(
                FakeSession(), GmailClient(http, FakeSession()), silent_only=silent_only
            )

    def broken_upgrade(path: Path) -> None:
        raise CommandError("Can't locate revision identified by 'unknown'")

    monkeypatch.setattr(gmail, "gmail_provider", factory)
    monkeypatch.setattr(gmail, "upgrade_database", broken_upgrade)
    args = ["sync", "--silent-only", "--database", str(tmp_path / "x.sqlite3"), "--timezone", "UTC"]

    assert gmail.main(args) == 5
    assert "Local database or file operation failed" in capsys.readouterr().out


BODY_MARKER = "BODY-MARKER-7741"


@pytest.mark.parametrize("show_text", [False, True])
def test_bodies_cli_reads_in_memory_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    respx_mock: respx.MockRouter,
    show_text: bool,
) -> None:
    @asynccontextmanager
    async def factory(
        settings: Settings, *, silent_only: bool = False
    ) -> AsyncIterator[GmailProvider]:
        async with httpx.AsyncClient() as http:
            yield GmailProvider(
                FakeSession(), GmailClient(http, FakeSession()), silent_only=silent_only
            )

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(gmail, "gmail_provider", factory)
    body = f"{BODY_MARKER} Please approve the budget.\n\nOn Monday Alex wrote:\n> OLD-QUOTED-TEXT"
    respx_mock.get(MESSAGES_URL + "/a1", params__contains={"format": "full"}).respond(
        json=message(part("text/plain", body), identifier="a1")
    )
    respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "a1"}]})
    respx_mock.get(MESSAGES_URL + "/a1").respond(json=metadata(received=datetime.now(UTC)))
    path = tmp_path / "bodies.sqlite3"
    args = ["bodies", "--silent-only", "--database", str(path), "--timezone", "UTC"]
    if show_text:
        args.append("--show-text")

    assert gmail.main(args) == 0

    output = capsys.readouterr().out
    assert "Sync status: complete; selected: 1" in output
    assert "1. ready; source: plain" in output and "quoted history removed" in output
    assert (BODY_MARKER in output) is show_text
    assert "OLD-QUOTED-TEXT" not in output
    assert BODY_MARKER not in caplog.text
    for stored in tmp_path.iterdir():
        assert BODY_MARKER.encode() not in stored.read_bytes()


def test_bodies_cli_reports_unreadable_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    respx_mock: respx.MockRouter,
) -> None:
    @asynccontextmanager
    async def factory(
        settings: Settings, *, silent_only: bool = False
    ) -> AsyncIterator[GmailProvider]:
        async with httpx.AsyncClient() as http:
            yield GmailProvider(
                FakeSession(), GmailClient(http, FakeSession()), silent_only=silent_only
            )

    monkeypatch.setattr(gmail, "gmail_provider", factory)
    respx_mock.get(MESSAGES_URL + "/a1", params__contains={"format": "full"}).respond(
        json={"id": "other", "payload": {}}
    )
    respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "a1"}]})
    respx_mock.get(MESSAGES_URL + "/a1").respond(json=metadata(received=datetime.now(UTC)))
    args = [
        "bodies",
        "--silent-only",
        "--database",
        str(tmp_path / "b.sqlite3"),
        "--timezone",
        "UTC",
    ]

    assert gmail.main(args) == 4
    assert "1. failed" in capsys.readouterr().out
