"""Desktop settings use background vault operations and account-scoped consent."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.errors import ConfigurationError
from mailbrief.providers.groq.credentials import GroqKeyStore
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, ConsentRepository
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
