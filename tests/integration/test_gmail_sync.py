"""Gmail metadata through real repositories/ranking; no bodies or AI calls."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.digests import SyncStatus
from mailbrief.providers.gmail.client import MESSAGES_URL, GmailClient
from mailbrief.providers.gmail.provider import GmailProvider
from mailbrief.services.application import ApplicationService
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, MessageRepository, SyncRunRepository
from tests.unit.providers.gmail.metadata_fixtures import (
    ACCOUNT,
    NOW,
    FakeSession,
    metadata,
    no_sleep,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    try:
        async with database.session() as session:
            yield session
    finally:
        await database.dispose()


async def test_sync_repeat_reconcile_and_review(
    session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    listing = respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "a"}, {"id": "b"}]})
    respx_mock.get(MESSAGES_URL + "/a").respond(json=metadata("a"))
    respx_mock.get(MESSAGES_URL + "/b").respond(json=metadata("b"))
    messages, accounts = MessageRepository(session), AccountRepository(session)
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession(), sleep=no_sleep))
        app = ApplicationService(provider, messages, SyncRunRepository(session), accounts)
        for _ in range(2):
            result, shortlist = await app.prepare_daily_shortlist(
                now_utc=NOW, tz_key="UTC", exclude_ids=("b",)
            )
            assert result.status is SyncStatus.COMPLETE
            assert result.message_count == 2
            assert [i.message.provider_message_id for i in shortlist] == ["a"]
        account = await accounts.get_by_provider_identity(
            ACCOUNT.provider, ACCOUNT.provider_account_id
        )
        assert account is not None
        rows = await messages.get_messages_in_range(
            account.id, NOW - timedelta(days=1), NOW + timedelta(days=1)
        )
        assert len(rows) == 2
        assert all(row.rank_score is not None for row in rows)
        # Archive b in Gmail: retained in cache, excluded from current Inbox/shortlist.
        listing.respond(json={"messages": [{"id": "a"}]})
        result, shortlist = await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
        rows = await messages.get_messages_in_range(
            account.id, NOW - timedelta(days=1), NOW + timedelta(days=1)
        )
        assert {row.provider_message_id: row.is_in_inbox for row in rows} == {"a": True, "b": False}
        assert len(shortlist) == 1
        # A completely empty successful scan reconciles the day too.
        listing.respond(json={})
        result, shortlist = await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
        assert result.status is SyncStatus.COMPLETE and shortlist == []
    assert all(call.request.method == "GET" for call in respx_mock.calls)
    assert all(
        call.request.url.params.get("format", "metadata") == "metadata" for call in respx_mock.calls
    )


async def test_partial_does_not_remove_unseen_cache_or_advance_success(
    session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    listing = respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "a"}]})
    respx_mock.get(MESSAGES_URL + "/a").respond(json=metadata("a"))
    messages, accounts, runs = (
        MessageRepository(session),
        AccountRepository(session),
        SyncRunRepository(session),
    )
    async with httpx.AsyncClient() as http:
        app = ApplicationService(
            GmailProvider(FakeSession(), GmailClient(http, FakeSession())), messages, runs, accounts
        )
        await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
        account = await accounts.get_by_provider_identity(
            ACCOUNT.provider, ACCOUNT.provider_account_id
        )
        assert account is not None
        await session.refresh(account)
        last_success = account.last_sync_at_utc
        listing.respond(json={"messages": [{"id": "broken"}]})
        respx_mock.get(MESSAGES_URL + "/broken").respond(json={})
        result, _ = await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
        assert result.status is SyncStatus.PARTIAL and result.failed_message_count == 1
        await session.refresh(account)
        assert account.last_sync_at_utc == last_success
        rows = await messages.get_messages_in_range(
            account.id, NOW - timedelta(days=1), NOW + timedelta(days=1), inbox_only=True
        )
        assert len(rows) == 1
        run = await runs.get_latest_sync_run(account.id)
        assert run and run.failed_message_count == 1


async def test_failed_second_page_preserves_first(
    session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(200, json={"messages": [{"id": "a"}], "nextPageToken": "next"}),
            httpx.Response(403),
        ]
    )
    respx_mock.get(MESSAGES_URL + "/a").respond(json=metadata("a"))
    async with httpx.AsyncClient() as http:
        app = ApplicationService(
            GmailProvider(FakeSession(), GmailClient(http, FakeSession())),
            MessageRepository(session),
            SyncRunRepository(session),
            AccountRepository(session),
        )
        result, shortlist = await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
    assert result.status is SyncStatus.PARTIAL
    assert result.message_count == 1 and len(shortlist) == 1
    assert result.error_code == "PERMISSION_DENIED"


async def test_ambiguous_no_content_preserves_cached_inbox_and_last_success(
    session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    listing = respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "a"}]})
    respx_mock.get(MESSAGES_URL + "/a").respond(json=metadata("a"))
    messages, accounts = MessageRepository(session), AccountRepository(session)
    async with httpx.AsyncClient() as http:
        app = ApplicationService(
            GmailProvider(FakeSession(), GmailClient(http, FakeSession())),
            messages,
            SyncRunRepository(session),
            accounts,
        )
        result, _ = await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
        assert result.status is SyncStatus.COMPLETE
        account = await accounts.get_by_provider_identity(
            ACCOUNT.provider, ACCOUNT.provider_account_id
        )
        assert account is not None
        await session.refresh(account)
        last_success = account.last_sync_at_utc
        listing.respond(204)
        result, _ = await app.prepare_daily_shortlist(now_utc=NOW, tz_key="UTC")
        assert result.status is SyncStatus.FAILED
        await session.refresh(account)
        assert account.last_sync_at_utc == last_success
        rows = await messages.get_messages_in_range(
            account.id, NOW - timedelta(days=1), NOW + timedelta(days=1), inbox_only=True
        )
        assert [row.provider_message_id for row in rows] == ["a"]


async def test_cancel_event_preserves_old_membership(
    session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(MESSAGES_URL).respond(json={})
    async with httpx.AsyncClient() as http:
        app = ApplicationService(
            GmailProvider(FakeSession(), GmailClient(http, FakeSession())),
            MessageRepository(session),
            SyncRunRepository(session),
            AccountRepository(session),
        )
        cancel = asyncio.Event()
        cancel.set()
        result, shortlist = await app.prepare_daily_shortlist(
            now_utc=NOW, tz_key="UTC", cancel=cancel
        )
    assert result.status is SyncStatus.CANCELLED and not shortlist
