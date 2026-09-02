"""Integration tests for the ApplicationService vertical slice."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailbrief.domain.digests import SyncProgress, SyncStage, SyncStatus
from mailbrief.domain.messages import (
    EmailContact,
    MessageImportance,
    NormalizedMessage,
)
from mailbrief.ports.errors import ProviderRateLimitError
from mailbrief.services.application import ApplicationService
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import Base
from tests.factories import make_message
from tests.unit.services.test_sync import FakeEmailProvider

TEST_NOW = datetime(2026, 8, 31, 14, 0, 0, tzinfo=UTC)
USER_EMAIL = "user@example.com"


@pytest.fixture
async def async_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def test_full_metadata_to_shortlist_slice(async_session: AsyncSession) -> None:
    # 40 messages: 5 high priority, 10 unread, 5 action language, rest normal
    messages_page_1: list[NormalizedMessage] = []
    for i in range(1, 21):
        importance = MessageImportance.HIGH if i <= 5 else MessageImportance.NORMAL
        subject = f"Urgent action required for project item {i}" if i <= 3 else f"Status update {i}"
        messages_page_1.append(
            make_message(
                provider_message_id=f"msg-{i:03d}",
                subject=subject,
                importance=importance,
                is_read=i > 10,
                received_at_utc=TEST_NOW - timedelta(minutes=i * 10),
            )
        )

    messages_page_2: list[NormalizedMessage] = []
    for i in range(21, 41):
        messages_page_2.append(
            make_message(
                provider_message_id=f"msg-{i:03d}",
                subject=f"Newsletter and alerts {i}",
                sender=EmailContact(name="Bot", address="no-reply@service.com"),
                importance=MessageImportance.LOW,
                received_at_utc=TEST_NOW - timedelta(minutes=i * 10),
            )
        )

    provider = FakeEmailProvider(pages=[messages_page_1, messages_page_2])
    msg_repo = MessageRepository(async_session)
    sync_run_repo = SyncRunRepository(async_session)
    account_repo = AccountRepository(async_session)

    app_service = ApplicationService(
        provider=provider,
        message_repo=msg_repo,
        sync_run_repo=sync_run_repo,
        account_repo=account_repo,
    )

    progress_events: list[SyncProgress] = []
    result, shortlist = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
        progress=progress_events.append,
    )

    # 1. Assert SyncResult
    assert result.status == SyncStatus.COMPLETE
    assert result.page_count == 2
    assert result.message_count == 40
    assert len(result.shortlisted_message_keys) == len(shortlist)
    assert 3 <= len(shortlist) <= 10
    assert any(p.stage == SyncStage.RANKING for p in progress_events)

    # 2. Check shortlist items are properly ranked
    assert shortlist[0].score > shortlist[-1].score or (shortlist[0].score == shortlist[-1].score)
    for ranked_item in shortlist:
        assert ranked_item.score >= 10  # high-scoring items qualified

    # 3. Check DB persistence of scores
    db_msgs = await msg_repo.get_messages_in_range(
        account_id=1,
        start_utc=TEST_NOW.replace(hour=0, minute=0, second=0),
        end_utc=TEST_NOW.replace(hour=0, minute=0, second=0) + timedelta(days=1),
    )
    assert len(db_msgs) == 40
    for row in db_msgs:
        assert row.rank_score is not None
        assert isinstance(row.rank_reasons_json, list)


async def test_empty_inbox_slice(async_session: AsyncSession) -> None:
    provider = FakeEmailProvider(pages=[[]])
    app_service = ApplicationService(
        provider=provider,
        message_repo=MessageRepository(async_session),
        sync_run_repo=SyncRunRepository(async_session),
        account_repo=AccountRepository(async_session),
    )

    result, shortlist = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
    )

    assert result.status == SyncStatus.COMPLETE
    assert result.message_count == 0
    assert result.shortlisted_message_keys == ()
    assert shortlist == []


async def test_cancellation_during_sync_slice(async_session: AsyncSession) -> None:
    page1 = [make_message(provider_message_id="msg-1")]
    page2 = [make_message(provider_message_id="msg-2")]
    provider = FakeEmailProvider(pages=[page1, page2])

    cancel_token = asyncio.Event()

    def cancel_after_page_1(p: SyncProgress) -> None:
        if p.pages_fetched == 1:
            cancel_token.set()

    app_service = ApplicationService(
        provider=provider,
        message_repo=MessageRepository(async_session),
        sync_run_repo=SyncRunRepository(async_session),
        account_repo=AccountRepository(async_session),
    )

    result, shortlist = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
        progress=cancel_after_page_1,
        cancel=cancel_token,
    )

    assert result.status == SyncStatus.CANCELLED
    assert shortlist == []


async def test_cancellation_before_ranking_slice(async_session: AsyncSession) -> None:
    page1 = [make_message(provider_message_id="msg-1")]
    provider = FakeEmailProvider(pages=[page1])

    cancel_token = asyncio.Event()

    def cancel_on_sync_complete(p: SyncProgress) -> None:
        if p.pages_fetched == 1:
            cancel_token.set()

    app_service = ApplicationService(
        provider=provider,
        message_repo=MessageRepository(async_session),
        sync_run_repo=SyncRunRepository(async_session),
        account_repo=AccountRepository(async_session),
    )

    # Pre-set cancel token so it's active before ranking
    cancel_token.set()
    result, shortlist = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
        cancel=cancel_token,
    )

    assert result.status == SyncStatus.CANCELLED
    assert shortlist == []


async def test_partial_sync_still_ranks_cached_messages(async_session: AsyncSession) -> None:
    page1 = [
        make_message(
            provider_message_id="msg-p1",
            subject="Action required on contract",
            importance=MessageImportance.HIGH,
            received_at_utc=TEST_NOW - timedelta(hours=1),
        )
    ]
    page2 = [make_message(provider_message_id="msg-p2")]

    provider = FakeEmailProvider(pages=[page1, page2])
    provider.fail_at_page = 2
    provider.failure_exception = ProviderRateLimitError("Rate limit reached")

    app_service = ApplicationService(
        provider=provider,
        message_repo=MessageRepository(async_session),
        sync_run_repo=SyncRunRepository(async_session),
        account_repo=AccountRepository(async_session),
    )

    result, shortlist = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
    )

    # Partial sync still allows ranking to process the cached page 1
    assert result.status == SyncStatus.PARTIAL
    assert result.message_count == 1
    assert len(shortlist) == 1
    assert shortlist[0].message.provider_message_id == "msg-p1"
    assert shortlist[0].score >= 20


async def test_pipeline_determinism(async_session: AsyncSession) -> None:
    msgs = [
        make_message(
            provider_message_id=f"det-{i}",
            subject=f"Task {i}",
            received_at_utc=TEST_NOW - timedelta(hours=i),
        )
        for i in range(1, 15)
    ]
    provider = FakeEmailProvider(pages=[msgs])

    app_service = ApplicationService(
        provider=provider,
        message_repo=MessageRepository(async_session),
        sync_run_repo=SyncRunRepository(async_session),
        account_repo=AccountRepository(async_session),
    )

    result_1, shortlist_1 = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
    )
    result_2, shortlist_2 = await app_service.prepare_daily_shortlist(
        tz_key="UTC",
        now_utc=TEST_NOW,
    )

    assert [m.message.provider_message_id for m in shortlist_1] == [
        m.message.provider_message_id for m in shortlist_2
    ]
    assert [m.score for m in shortlist_1] == [m.score for m in shortlist_2]
    assert result_1.shortlisted_message_keys == result_2.shortlisted_message_keys
