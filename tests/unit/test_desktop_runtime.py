"""Desktop settings use background vault operations and account-scoped consent."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import time_machine
from pydantic import SecretStr
from sqlalchemy import text, update

from mailbrief.config import Settings
from mailbrief.domain.digests import DigestStatus
from mailbrief.domain.drafting import DraftingOptions
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.domain.preferences import AI_LIMIT_FIELDS, PreferencesEdit
from mailbrief.errors import ConfigurationError
from mailbrief.providers.groq.credentials import GroqKeyStore
from mailbrief.services.calendar import resolve_timezone
from mailbrief.services.history import BriefDateError
from mailbrief.services.preferences import PreferencesConflictError, PreferencesUnavailableError
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, ConsentRepository, DigestRepository
from mailbrief.storage.tables import DigestTable
from mailbrief.ui import runtime
from mailbrief.ui.preferences import DesktopPreferences
from mailbrief.ui.runtime import DesktopRuntime
from tests.unit.providers.groq.groq_fixtures import TEST_KEY, MemoryVault


async def test_cancelled_startup_joins_migration_worker_before_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    disposed = AsyncMock()

    def migrate(path: Path) -> None:
        started.set()
        assert released.wait(timeout=5)
        finished.set()

    monkeypatch.setattr(runtime, "upgrade_database", migrate)
    monkeypatch.setattr(Database, "dispose", disposed)
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    task = asyncio.create_task(backend.load_saved())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        disposed.assert_awaited_once()
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)
        await backend.close()


async def test_settings_persist_without_copying_oauth_secrets(tmp_path: Path) -> None:
    client = tmp_path / "oauth.json"
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test.apps.googleusercontent.com",
                    "client_secret": "OAUTH_SECRET_MARKER",
                }
            }
        ),
        encoding="utf-8",
    )
    backend = DesktopRuntime(tmp_path / "profile" / "mailbrief.sqlite3")
    preferences = DesktopPreferences(gmail_oauth_client_path=client, groq_model="model-one")
    await backend.save_preferences(preferences)
    restored = DesktopRuntime(tmp_path / "profile" / "mailbrief.sqlite3")
    assert await restored.get_preferences() == preferences
    text = (tmp_path / "profile" / "desktop-settings.json").read_text(encoding="utf-8")
    assert "OAUTH_SECRET_MARKER" not in text
    with pytest.raises(ConfigurationError):
        await backend.save_preferences(DesktopPreferences(gmail_oauth_client_path=tmp_path / "no"))
    assert await restored.get_preferences() == preferences


async def test_vault_writes_stay_off_main_thread_and_out_of_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: list[int] = []
    store = GroqKeyStore(MemoryVault())

    def vault() -> GroqKeyStore:
        threads.append(threading.get_ident())
        return store

    monkeypatch.setattr(runtime, "GroqKeyStore", vault)
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.save_preferences(DesktopPreferences(groq_model="test-model"))
    assert "key missing" in await backend.ai_status()
    await backend.save_key(SecretStr(TEST_KEY))
    assert "configured" in await backend.ai_status()
    assert TEST_KEY not in (tmp_path / "desktop-settings.json").read_text()
    await backend.remove_key()
    assert store.load() is None
    assert threads and threading.get_ident() not in threads


async def test_revoke_only_groq_for_gmail_accounts(tmp_path: Path) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    backend = DesktopRuntime(path)
    await backend.load_saved()
    database = Database.from_path(path)
    identities: dict[ProviderKind, int] = {}
    try:
        async with database.transaction() as session:
            for kind in (ProviderKind.GMAIL, ProviderKind.MICROSOFT):
                account = await AccountRepository(session).upsert(
                    AccountIdentity(
                        provider=kind,
                        provider_account_id=kind.value,
                        email_address=f"{kind.value}@example.com",
                        display_name="Test",
                    )
                )
                identities[kind] = account.id
                for provider in ("groq", "openai"):
                    await ConsentRepository(session).grant(
                        account.id,
                        provider,
                        "test",
                        datetime.now(UTC),
                    )
        assert await backend.revoke_consent() == 1
        assert await backend.revoke_consent() == 0
        async with database.session() as session:
            consents = ConsentRepository(session)
            assert await consents.get_active(identities[ProviderKind.GMAIL], "groq", "test") is None
            assert await consents.get_active(identities[ProviderKind.GMAIL], "openai", "test")
            assert await consents.get_active(identities[ProviderKind.MICROSOFT], "groq", "test")
    finally:
        await database.dispose()
        await backend.close()


class Recorder:
    """Stands in for a service or provider factory and records how it was built and used."""

    def __init__(self) -> None:
        self.built: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "Recorder":
        self.built.append({"args": args, **kwargs})
        return self

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "result"

    async def available_parts(self, public_id: str) -> frozenset[str]:
        return frozenset()

    async def prepare(self, public_id: str, options: object) -> str:
        return "plan"


def provider_factory(settings_seen: list[Settings]) -> Any:
    @asynccontextmanager
    async def factory(settings: Settings, **kwargs: Any) -> AsyncIterator[object]:
        settings_seen.append(settings)
        yield object()

    return factory


@pytest.fixture
def ai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AI_LIMIT_FIELDS:
        monkeypatch.delenv(f"MAILBRIEF_{name.upper()}", raising=False)
    monkeypatch.setenv("MAILBRIEF_AI_TIMEOUT_SECONDS", "30")


PLAN: Any = "plan"  # The drafting service is a Recorder, so any plan will do.
SAVED = PreferencesEdit(
    time_zone="Asia/Tokyo",
    shortlist_limit=3,
    excluded_senders=("@example.org",),
    ai_batch_size=4,
    ai_body_character_limit=2_000,
    ai_timeout_seconds=90,
)


async def test_a_brief_uses_the_owner_s_zone_limit_rules_and_ai_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ai_environment: None
) -> None:
    seen: list[Settings] = []
    monkeypatch.setattr(runtime, "gmail_provider", provider_factory(seen))
    monkeypatch.setattr(runtime, "groq_provider", provider_factory(seen))
    brief, bodies, analysis = Recorder(), Recorder(), Recorder()
    monkeypatch.setattr(runtime, "BriefService", brief)
    monkeypatch.setattr(runtime, "BodyService", bodies)
    monkeypatch.setattr(runtime, "AnalysisService", analysis)
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.load_saved()
    try:
        await backend.save_owner_preferences(SAVED, 0)
        review = AsyncMock()

        result: object = await backend.generate(
            AsyncMock(), review, asyncio.Event(), lambda _: None
        )

        assert result == "result"
        (call,) = brief.calls
        assert call["tz_key"] == "Asia/Tokyo"
        assert call["shortlist_limit"] == 3
        assert call["excluded_senders"] == ("@example.org",)
        assert call["shortlist_gate"] is review
        assert bodies.built[0]["limit"] == 2_000  # Saved.
        assert analysis.built[0]["batch_size"] == 4  # Saved.
        assert {settings.ai_timeout_seconds for settings in seen} == {30}  # Environment.
    finally:
        await backend.close()


async def test_drafting_uses_the_owner_s_zone_and_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ai_environment: None
) -> None:
    monkeypatch.setattr(runtime, "groq_provider", provider_factory([]))
    drafting = Recorder()
    monkeypatch.setattr(runtime, "DraftingService", drafting)
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.load_saved()
    try:
        await backend.save_owner_preferences(SAVED, 0)

        await backend.available_drafting_parts("draft")
        await backend.prepare_drafting("draft", DraftingOptions())
        await backend.generate_draft(PLAN, AsyncMock(), asyncio.Event())

        assert len(drafting.built) == 3
        for built in drafting.built:
            assert built["zone"] == ZoneInfo("Asia/Tokyo")
            assert built["excluded_senders"] == ("@example.org",)
    finally:
        await backend.close()


async def test_unreadable_preferences_stop_everything_that_could_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = AsyncMock(side_effect=AssertionError("no provider may be built"))
    monkeypatch.setattr(runtime, "gmail_provider", forbidden)
    monkeypatch.setattr(runtime, "groq_provider", forbidden)
    path = tmp_path / "mailbrief.sqlite3"
    backend = DesktopRuntime(path)
    await backend.load_saved()
    try:
        await backend.save_owner_preferences(SAVED, 0)
        database = Database.from_path(path)
        try:
            async with database.transaction() as session:
                await session.execute(
                    text("UPDATE owner_preferences SET excluded_senders_json = 'not json'")
                )
        finally:
            await database.dispose()

        operations = (
            backend.generate(AsyncMock(), AsyncMock(), asyncio.Event(), lambda _: None),
            backend.available_drafting_parts("draft"),
            backend.prepare_drafting("draft", DraftingOptions()),
            backend.generate_draft(PLAN, AsyncMock(), asyncio.Event()),
        )
        for operation in operations:
            with pytest.raises(PreferencesUnavailableError):
                await operation

        assert (await backend.reset_owner_preferences()).excluded_senders == ()
        assert (await backend.get_owner_preferences()).revision == 2
    finally:
        await backend.close()


async def test_a_stale_save_is_a_conflict(tmp_path: Path) -> None:
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.load_saved()
    try:
        assert (await backend.get_owner_preferences()).revision == 0
        await backend.save_owner_preferences(PreferencesEdit(shortlist_limit=5), 0)
        with pytest.raises(PreferencesConflictError):
            await backend.save_owner_preferences(PreferencesEdit(shortlist_limit=6), 0)
        assert (await backend.get_owner_preferences()).shortlist_limit == 5
    finally:
        await backend.close()


async def seed_briefs(path: Path) -> None:
    """Today's brief, then a brief for yesterday saved later, as after catching up."""
    database = Database.from_path(path)
    try:
        async with database.transaction() as session:
            account = await AccountRepository(session).upsert(
                AccountIdentity(
                    provider=ProviderKind.GMAIL,
                    provider_account_id="gmail-1",
                    email_address="owner@example.com",
                )
            )
            for day, hour in ((date(2026, 9, 29), 13), (date(2026, 9, 28), 16)):
                digest = await DigestRepository(session).save_digest(
                    account_id=account.id,
                    local_date=day,
                    timezone_name="UTC",
                    status=DigestStatus.EMPTY,
                )
                await session.execute(
                    update(DigestTable)
                    .where(DigestTable.id == digest.id)
                    .values(generated_at_utc=datetime(2026, 9, 29, hour, tzinfo=UTC))
                )
    finally:
        await database.dispose()


@time_machine.travel(datetime(2026, 9, 29, 18, tzinfo=UTC), tick=False)
async def test_startup_shows_today_s_brief_after_catching_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("UTC"))
    path = tmp_path / "mailbrief.sqlite3"
    backend = DesktopRuntime(path)
    await backend.load_saved()
    try:
        await seed_briefs(path)

        latest = await backend.load_saved()
        listed = await backend.list_briefs()
        past = await backend.load_brief("owner@example.com", date(2026, 9, 28))
        missed = await backend.missed_days("owner@example.com")

        assert latest is not None and latest.local_date == date(2026, 9, 29)
        assert [summary.local_date for summary in listed] == [
            date(2026, 9, 29),
            date(2026, 9, 28),
        ]
        assert past is not None and past.local_date == date(2026, 9, 28)
        assert missed == tuple(date(2026, 9, day) for day in (27, 26, 25, 24, 23, 22))
    finally:
        await backend.close()


async def test_an_out_of_range_day_stops_before_any_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = AsyncMock(side_effect=AssertionError("no provider may be built"))
    monkeypatch.setattr(runtime, "gmail_provider", forbidden)
    monkeypatch.setattr(runtime, "groq_provider", forbidden)
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.load_saved()
    try:
        with pytest.raises(BriefDateError):
            await backend.generate(
                AsyncMock(), AsyncMock(), asyncio.Event(), lambda _: None, local_date=date.min
            )
    finally:
        await backend.close()


async def test_a_past_day_reaches_the_brief_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "gmail_provider", provider_factory([]))
    monkeypatch.setattr(runtime, "groq_provider", provider_factory([]))
    brief = Recorder()
    monkeypatch.setattr(runtime, "BriefService", brief)
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.load_saved()
    try:
        yesterday = datetime.now(UTC).astimezone(resolve_timezone(None)).date() - timedelta(1)
        await backend.generate(
            AsyncMock(), AsyncMock(), asyncio.Event(), lambda _: None, local_date=yesterday
        )
        assert brief.calls[0]["local_date"] == yesterday
    finally:
        await backend.close()
