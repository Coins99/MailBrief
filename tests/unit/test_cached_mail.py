"""Offline pagination is account-scoped, date-bounded and independent of credentials."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.domain.preferences import PreferencesEdit
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.ui import runtime
from mailbrief.ui.runtime import DesktopRuntime
from tests.factories import make_message


async def test_offline_pages_include_archived_metadata_and_respect_dst_and_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Mock(
        side_effect=AssertionError("Offline browsing must not access a provider or vault")
    )
    monkeypatch.setattr(runtime, "gmail_provider", forbidden)
    monkeypatch.setattr(runtime, "groq_provider", forbidden)
    monkeypatch.setattr(runtime, "GroqKeyStore", forbidden)
    monkeypatch.setattr(runtime, "GmailCredentialStore", forbidden)
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("Asia/Tokyo"))
    path = tmp_path / "mailbrief.sqlite3"
    backend = DesktopRuntime(path)
    await backend.load_saved()
    # The owner's saved zone decides the day, not the system zone.
    await backend.save_owner_preferences(PreferencesEdit(time_zone="America/Toronto"), 0)
    database = Database.from_path(path)
    start = datetime(2026, 11, 1, 4, tzinfo=UTC)  # A 25-hour local day.
    finish = start + timedelta(hours=25)
    identities: list[int] = []
    try:
        async with database.transaction() as session:
            for index, provider in enumerate(
                (ProviderKind.GMAIL, ProviderKind.GMAIL, ProviderKind.MICROSOFT)
            ):
                account = await AccountRepository(session).upsert(
                    AccountIdentity(
                        provider=provider,
                        provider_account_id=f"account-{index}",
                        email_address=f"owner{index}@example.com",
                    )
                )
                identities.append(account.id)
                messages = [
                    make_message(
                        provider=provider,
                        provider_account_id=f"account-{index}",
                        provider_message_id=f"m{number}",
                        received_at_utc=start + timedelta(minutes=number),
                        is_in_inbox=number != 0,
                    )
                    for number in range(101 if index == 0 else 1)
                ]
                messages.extend(
                    [
                        make_message(
                            provider=provider,
                            provider_account_id=f"account-{index}",
                            provider_message_id="last-minute",
                            received_at_utc=finish - timedelta(minutes=1),
                        ),
                        make_message(
                            provider=provider,
                            provider_account_id=f"account-{index}",
                            provider_message_id="next-day",
                            received_at_utc=finish,
                        ),
                    ]
                )
                await MessageRepository(session).upsert_messages(account.id, messages)
        assert len(await backend.cached_accounts()) == 2
        first = await backend.cached_messages(identities[0], date(2026, 11, 1))
        assert len(first.messages) == 100 and first.has_more
        assert first.messages[0].provider_message_id == "last-minute"
        second = await backend.cached_messages(identities[0], date(2026, 11, 1), 100)
        assert len(second.messages) == 2 and not second.has_more
        all_messages = first.messages + second.messages
        assert len({item.provider_message_id for item in all_messages}) == 102
        assert all(item.provider_account_id == "account-0" for item in all_messages)
        assert not second.messages[-1].is_in_inbox
        assert len((await backend.cached_messages(identities[1], date(2026, 11, 1))).messages) == 2
        with pytest.raises(ValueError):
            await backend.cached_messages(identities[2], date(2026, 11, 1))
        with pytest.raises(ValueError):
            await backend.cached_messages(identities[0], date(2026, 11, 1), -1)
        forbidden.assert_not_called()
    finally:
        await database.dispose()
        await backend.close()
    # A new runtime reads the same metadata without any connection restoration.
    reopened = DesktopRuntime(path)
    try:
        await reopened.load_saved()
        assert (
            len((await reopened.cached_messages(identities[0], date(2026, 11, 1))).messages) == 100
        )
        forbidden.assert_not_called()
    finally:
        await reopened.close()


async def test_offline_browsing_falls_back_to_the_system_zone_when_preferences_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browsing only displays local data, so it never fails closed."""
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("Asia/Tokyo"))
    path = tmp_path / "mailbrief.sqlite3"
    backend = DesktopRuntime(path)
    await backend.load_saved()
    await backend.save_owner_preferences(PreferencesEdit(time_zone="America/Toronto"), 0)
    database = Database.from_path(path)
    try:
        async with database.transaction() as session:
            account = await AccountRepository(session).upsert(
                AccountIdentity(
                    provider=ProviderKind.GMAIL,
                    provider_account_id="account-0",
                    email_address="owner@example.com",
                )
            )
            await session.execute(
                text("UPDATE owner_preferences SET excluded_senders_json = 'not json'")
            )
        page = await backend.cached_messages(account.id, date(2026, 11, 1))
        assert page.timezone_name == "Asia/Tokyo"
    finally:
        await database.dispose()
        await backend.close()
