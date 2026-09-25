"""Real migration/database/CLI composition with synthetic Gmail metadata."""

from collections.abc import AsyncIterator
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
