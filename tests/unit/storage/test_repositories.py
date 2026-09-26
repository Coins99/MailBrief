"""Comprehensive tests for async storage repositories."""

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from mailbrief.domain.analysis import AnalysisCategory, DeadlinePrecision, MessageAnalysis
from mailbrief.domain.digests import DigestCoverage, DigestSection, DigestStatus, SyncStatus
from mailbrief.domain.messages import (
    AccountIdentity,
    MessageImportance,
    NormalizedMessage,
    ProviderKind,
    RankReason,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    AnalysisRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import MessageTable
from tests.factories import make_analysis, make_message


@pytest.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    """Provide a fresh SQLite database with created schema."""
    db = Database.from_path(tmp_path / "test.sqlite3")
    await db.create_schema_for_tests()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_account_repository_upsert_and_queries(database: Database) -> None:
    identity = AccountIdentity(
        provider=ProviderKind.MICROSOFT,
        provider_account_id="ms-user-123",
        email_address="user@example.com",
        display_name="User One",
        tenant_id="tenant-abc",
    )

    async with database.transaction() as session:
        repo = AccountRepository(session)
        account = await repo.upsert(identity)
        assert account.id is not None
        assert account.provider == "microsoft"
        assert account.email_address == "user@example.com"
        assert account.display_name == "User One"
        assert account.created_at_utc.tzinfo is UTC

    async with database.session() as session:
        repo = AccountRepository(session)
        fetched = await repo.get_by_provider_identity(ProviderKind.MICROSOFT, "ms-user-123")
        assert fetched is not None
        assert fetched.id == account.id
        assert fetched.display_name == "User One"

        by_id = await repo.get_by_id(account.id)
        assert by_id is not None
        assert by_id.provider_account_id == "ms-user-123"

        all_accounts = await repo.list_all()
        assert len(all_accounts) == 1

    # Test updating account attributes on subsequent upsert
    updated_identity = AccountIdentity(
        provider=ProviderKind.MICROSOFT,
        provider_account_id="ms-user-123",
        email_address="user-updated@example.com",
        display_name="User Updated",
        tenant_id="tenant-xyz",
    )
    async with database.transaction() as session:
        repo = AccountRepository(session)
        updated_account = await repo.upsert(updated_identity)
        assert updated_account.id == account.id
        assert updated_account.email_address == "user-updated@example.com"
        assert updated_account.display_name == "User Updated"

    # Test update_last_sync
    sync_time = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
    async with database.transaction() as session:
        repo = AccountRepository(session)
        await repo.update_last_sync(account.id, sync_time)

    async with database.session() as session:
        repo = AccountRepository(session)
        fetched = await repo.get_by_id(account.id)
        assert fetched is not None
        assert fetched.last_sync_at_utc == sync_time

    # Test count and get_by_email
    assert await repo.count() == 1
    by_email = await repo.get_by_email("user-updated@example.com")
    assert by_email is not None
    assert by_email.id == account.id

    # Test delete
    async with database.transaction() as session:
        repo = AccountRepository(session)
        deleted = await repo.delete_by_id(account.id)
        assert deleted is True

    async with database.session() as session:
        repo = AccountRepository(session)
        assert await repo.get_by_id(account.id) is None
        assert await repo.count() == 0


@pytest.mark.asyncio
async def test_message_repository_upsert_and_ranking(database: Database) -> None:
    identity = AccountIdentity(
        provider=ProviderKind.MICROSOFT,
        provider_account_id="ms-user-123",
        email_address="user@example.com",
    )
    async with database.transaction() as session:
        account = await AccountRepository(session).upsert(identity)
        account_id = account.id

    msg1 = make_message(
        provider_message_id="msg-001",
        subject="Important Announcement",
        received_at_utc=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        importance=MessageImportance.HIGH,
    )
    msg2 = make_message(
        provider_message_id="msg-002",
        subject="Lunch plans",
        received_at_utc=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        importance=MessageImportance.LOW,
    )

    # Bulk upsert including in-batch duplicates (should deduplicate gracefully)
    async with database.transaction() as session:
        repo = MessageRepository(session)
        saved = await repo.upsert_messages(account_id, [msg1, msg2, msg1])
        assert len(saved) == 2

    # Empty list handles gracefully
    async with database.transaction() as session:
        repo = MessageRepository(session)
        assert await repo.upsert_messages(account_id, []) == []

    # Idempotent re-upsert with modified subject
    msg1_modified = make_message(
        provider_message_id="msg-001",
        subject="Important Announcement (Updated)",
        received_at_utc=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
    )
    async with database.transaction() as session:
        repo = MessageRepository(session)
        saved = await repo.upsert_messages(account_id, [msg1_modified])
        assert len(saved) == 1
        assert saved[0].subject == "Important Announcement (Updated)"

    async with database.session() as session:
        repo = MessageRepository(session)
        # Total messages count should still be 2 (no duplicates)
        count = await session.scalar(select(func.count()).select_from(MessageTable))
        assert count == 2

        # Query range
        in_range = await repo.get_messages_in_range(
            account_id,
            datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        )
        assert len(in_range) == 2
        # Ordered by received_at_utc desc
        assert in_range[0].provider_message_id == "msg-002"
        assert in_range[1].provider_message_id == "msg-001"

        # Out of range query
        empty_range = await repo.get_messages_in_range(
            account_id,
            datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
        )
        assert len(empty_range) == 0

        # Query by provider message id
        fetched = await repo.get_by_provider_message_id(account_id, "msg-001")
        assert fetched is not None
        assert fetched.subject == "Important Announcement (Updated)"

        # Domain mapping test
        domain_msg = MessageRepository.to_domain(
            fetched,
            provider_account_id="ms-user-123",
            provider=ProviderKind.MICROSOFT,
        )
        assert isinstance(domain_msg, NormalizedMessage)
        assert domain_msg.provider_message_id == "msg-001"
        assert domain_msg.subject == "Important Announcement (Updated)"

    # Batch update ranking score and reasons
    async with database.transaction() as session:
        repo = MessageRepository(session)
        msg1_row = await repo.get_by_provider_message_id(account_id, "msg-001")
        msg2_row = await repo.get_by_provider_message_id(account_id, "msg-002")
        assert msg1_row is not None and msg2_row is not None
        await repo.update_rankings(
            [
                (msg1_row.id, 50, [RankReason.HIGH_IMPORTANCE]),
                (msg2_row.id, -5, [RankReason.LOW_IMPORTANCE]),
            ]
        )
        await repo.update_rankings([])  # Empty batch handled gracefully

    async with database.session() as session:
        repo = MessageRepository(session)
        f1 = await repo.get_by_provider_message_id(account_id, "msg-001")
        f2 = await repo.get_by_provider_message_id(account_id, "msg-002")
        assert f1 is not None and f1.rank_score == 50
        assert f2 is not None and f2.rank_score == -5

    # Batch update by provider_message_id
    async with database.transaction() as session:
        repo = MessageRepository(session)
        await repo.update_rankings_by_provider_id(
            account_id,
            [
                ("msg-001", 60, [RankReason.VERY_RECENT]),
                ("msg-002", 15, [RankReason.RECENT]),
            ],
        )
        await repo.update_rankings_by_provider_id(account_id, [])

    async with database.session() as session:
        repo = MessageRepository(session)
        f1 = await repo.get_by_provider_message_id(account_id, "msg-001")
        f2 = await repo.get_by_provider_message_id(account_id, "msg-002")
        assert f1 is not None and f1.rank_score == 60 and f1.rank_reasons_json == ["very_recent"]
        assert f2 is not None and f2.rank_score == 15 and f2.rank_reasons_json == ["recent"]


@pytest.mark.asyncio
async def test_analysis_repository_caching_and_queries(database: Database) -> None:
    async with database.transaction() as session:
        account = await AccountRepository(session).upsert(
            AccountIdentity(
                provider=ProviderKind.MICROSOFT,
                provider_account_id="ms-1",
                email_address="a@example.com",
            )
        )
        msg = (
            await MessageRepository(session).upsert_messages(
                account.id,
                [make_message(provider_message_id="msg-1")],
            )
        )[0]
        msg_id = msg.id

    analysis_domain = make_analysis(
        category=AnalysisCategory.ACTION,
        summary="Submit quarterly budget report",
        action_required=True,
        action_text="Submit budget report by Friday 5 PM",
        deadline_text="Friday 5 PM",
        deadline_at_utc=datetime(2026, 9, 4, 17, 0, tzinfo=UTC),
        confidence=0.95,
        evidence="Please submit quarterly budget report by Friday 5 PM.",
    )

    # Insert analysis
    async with database.transaction() as session:
        repo = AnalysisRepository(session)
        saved = await repo.upsert_analysis(
            message_id=msg_id,
            input_hash="hash-123456",
            provider="openai",
            model="gpt-4o-mini",
            prompt_version="v1",
            schema_version="v1",
            analysis=analysis_domain,
        )
        assert saved.id is not None
        assert saved.message_id == msg_id
        assert saved.summary == "Submit quarterly budget report"
        assert saved.action_required is True
        assert saved.confidence == 0.95

    # Check cache hit
    async with database.session() as session:
        repo = AnalysisRepository(session)
        cached = await repo.get_cached_analysis(
            message_id=msg_id,
            input_hash="hash-123456",
            provider="openai",
            model="gpt-4o-mini",
            prompt_version="v1",
            schema_version="v1",
        )
        assert cached is not None
        assert cached.summary == "Submit quarterly budget report"

        # Cache miss on different hash or version
        miss = await repo.get_cached_analysis(
            message_id=msg_id,
            input_hash="different-hash",
            provider="openai",
            model="gpt-4o-mini",
            prompt_version="v1",
            schema_version="v1",
        )
        assert miss is None

        # Domain mapping test
        mapped = AnalysisRepository.to_domain(cached, message_key="msg-1")
        assert isinstance(mapped, MessageAnalysis)
        assert mapped.summary == "Submit quarterly budget report"
        assert mapped.action_required is True

        # List by message id
        analyses = await repo.get_by_message_id(msg_id)
        assert len(analyses) == 1


@pytest.mark.asyncio
async def test_digest_repository_saving_and_membership(database: Database) -> None:
    async with database.transaction() as session:
        account = await AccountRepository(session).upsert(
            AccountIdentity(
                provider=ProviderKind.MICROSOFT,
                provider_account_id="ms-1",
                email_address="a@example.com",
            )
        )
        msgs = await MessageRepository(session).upsert_messages(
            account.id,
            [
                make_message(provider_message_id="msg-1", subject="Action message"),
                make_message(provider_message_id="msg-2", subject="Highlight message"),
            ],
        )
        msg1_id, msg2_id = msgs[0].id, msgs[1].id

        analysis = await AnalysisRepository(session).upsert_analysis(
            message_id=msg1_id,
            input_hash="hash-1",
            provider="openai",
            model="gpt-4o",
            prompt_version="v1",
            schema_version="v1",
            analysis=make_analysis(),
        )
        analysis_id = analysis.id
        account_id = account.id

    test_date = date(2026, 8, 31)

    # Save digest with items
    async with database.transaction() as session:
        repo = DigestRepository(session)
        digest = await repo.save_digest(
            account_id=account_id,
            local_date=test_date,
            timezone_name="America/New_York",
            status=DigestStatus.COMPLETE,
            items=[
                (msg1_id, analysis_id, 0, DigestSection.ACTIONS),
                (msg2_id, None, 1, DigestSection.HIGHLIGHTS),
            ],
        )
        assert digest.id is not None
        assert digest.status == "complete"
        assert digest.timezone_name == "America/New_York"

    # Query digest and items
    async with database.session() as session:
        repo = DigestRepository(session)
        fetched_digest = await repo.get_by_account_and_date(account_id, test_date)
        assert fetched_digest is not None
        assert fetched_digest.id == digest.id

        items = await repo.get_digest_items(digest.id)
        assert len(items) == 2
        # Position 0: msg1 with analysis
        item0, msg_row0, analysis_row0 = items[0]
        assert item0.position == 0
        assert item0.section == "actions"
        assert msg_row0.id == msg1_id
        assert analysis_row0 is not None
        assert analysis_row0.id == analysis_id

        # Position 1: msg2 without analysis
        item1, msg_row1, analysis_row1 = items[1]
        assert item1.position == 1
        assert item1.section == "highlights"
        assert msg_row1.id == msg2_id
        assert analysis_row1 is None

        # Test to_domain reconstruction
        daily_digest = DigestRepository.to_domain(
            fetched_digest,
            items,
            account_identity="ms-1",
        )
        assert daily_digest.status == DigestStatus.COMPLETE
        assert len(daily_digest.items) == 2
        assert daily_digest.items[0].section == DigestSection.ACTIONS
        assert daily_digest.items[1].section == DigestSection.HIGHLIGHTS

    # Idempotently update digest replacing items
    async with database.transaction() as session:
        repo = DigestRepository(session)
        updated_digest = await repo.save_digest(
            account_id=account_id,
            local_date=test_date,
            timezone_name="America/New_York",
            status=DigestStatus.PARTIAL,
            items=[(msg1_id, analysis_id, 0, DigestSection.DEADLINES)],
        )
        assert updated_digest.id == digest.id
        assert updated_digest.status == "partial"

    async with database.session() as session:
        repo = DigestRepository(session)
        items = await repo.get_digest_items(digest.id)
        assert len(items) == 1
        assert items[0][0].section == "deadlines"
        assert await repo.get_by_id(digest.id) is not None


@pytest.mark.asyncio
async def test_sync_run_repository_lifecycle(database: Database) -> None:
    async with database.transaction() as session:
        account = await AccountRepository(session).upsert(
            AccountIdentity(
                provider=ProviderKind.MICROSOFT,
                provider_account_id="ms-1",
                email_address="a@example.com",
            )
        )
        account_id = account.id

    start_utc = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
    end_utc = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

    # Create initial sync run
    async with database.transaction() as session:
        repo = SyncRunRepository(session)
        sync_run = await repo.create_sync_run(
            account_id=account_id,
            range_start_utc=start_utc,
            range_end_utc=end_utc,
            status="started",
        )
        assert sync_run.id is not None
        assert sync_run.status == "started"
        assert sync_run.page_count == 0
        sync_run_id = sync_run.id

    # Update progress
    async with database.transaction() as session:
        repo = SyncRunRepository(session)
        updated = await repo.update_sync_run(
            sync_run_id,
            status=SyncStatus.PARTIAL,
            page_count=3,
            message_count=120,
        )
        assert updated is not None
        assert updated.page_count == 3
        assert updated.message_count == 120

    # Mark completed
    async with database.transaction() as session:
        repo = SyncRunRepository(session)
        completed_run = await repo.update_sync_run(
            sync_run_id,
            status=SyncStatus.COMPLETE,
            completed=True,
        )
        assert completed_run is not None
        assert completed_run.status == "complete"
        assert completed_run.completed_at_utc is not None

    # Query latest sync run
    async with database.session() as session:
        repo = SyncRunRepository(session)
        latest = await repo.get_latest_sync_run(account_id)
        assert latest is not None
        assert latest.id == sync_run_id
        assert latest.status == "complete"
        assert await repo.get_by_id(sync_run_id) is not None


async def _account_and_messages(database: Database, *message_ids: str) -> tuple[int, list[int]]:
    async with database.transaction() as session:
        account = await AccountRepository(session).upsert(
            AccountIdentity(
                provider=ProviderKind.MICROSOFT,
                provider_account_id="ms-1",
                email_address="a@example.com",
            )
        )
        messages = await MessageRepository(session).upsert_messages(
            account.id,
            [make_message(provider_message_id=message_id) for message_id in message_ids],
        )
        return account.id, [message.id for message in messages]


@pytest.mark.asyncio
async def test_analysis_cache_identity_separates_provider_and_schema(database: Database) -> None:
    _, (message_id,) = await _account_and_messages(database, "msg-1")
    identities = [("openai", "2"), ("other-ai", "2"), ("openai", "3")]

    async with database.transaction() as session:
        repo = AnalysisRepository(session)
        for provider, schema_version in identities:
            await repo.upsert_analysis(
                message_id=message_id,
                input_hash="hash-1",
                provider=provider,
                model="model-1",
                prompt_version="prompt-1",
                schema_version=schema_version,
                analysis=make_analysis(summary=f"{provider} {schema_version}"),
            )

    async with database.transaction() as session:
        await AnalysisRepository(session).upsert_analysis(
            message_id=message_id,
            input_hash="hash-1",
            provider="openai",
            model="model-1",
            prompt_version="prompt-1",
            schema_version="2",
            analysis=make_analysis(
                summary="updated",
                deadline_text="ASAP",
                deadline_precision=DeadlinePrecision.UNRESOLVED,
                deadline_date=None,
                deadline_at_utc=None,
                deadline_timezone=None,
            ),
        )

    async with database.session() as session:
        repo = AnalysisRepository(session)
        assert len(await repo.get_by_message_id(message_id)) == 3
        found: dict[tuple[str, str], MessageAnalysis] = {}
        for provider, schema_version in identities:
            row = await repo.get_cached_analysis(
                message_id=message_id,
                input_hash="hash-1",
                provider=provider,
                model="model-1",
                prompt_version="prompt-1",
                schema_version=schema_version,
            )
            assert row is not None
            found[(provider, schema_version)] = AnalysisRepository.to_domain(row, "local-1")
        missing = await repo.get_cached_analysis(
            message_id=message_id,
            input_hash="hash-1",
            provider="openai",
            model="model-1",
            prompt_version="prompt-1",
            schema_version="1",
        )

    assert missing is None
    assert found[("other-ai", "2")].summary == "other-ai 2"
    assert found[("openai", "3")].summary == "openai 3"
    exact = found[("openai", "3")]
    assert exact.deadline_precision is DeadlinePrecision.DATETIME
    assert exact.deadline_date == date(2026, 9, 4)
    assert exact.deadline_timezone == "America/Toronto"
    updated = found[("openai", "2")]
    assert updated.summary == "updated"
    assert updated.deadline_precision is DeadlinePrecision.UNRESOLVED
    assert updated.deadline_date is None
    assert updated.deadline_timezone is None


@pytest.mark.asyncio
async def test_digest_round_trip_with_coverage_and_deadline_details(database: Database) -> None:
    account_id, (first_id, second_id) = await _account_and_messages(database, "msg-1", "msg-2")
    coverage = DigestCoverage(
        sync_complete=False,
        shortlisted=3,
        analyzed=1,
        reused=1,
        failed=0,
        skipped=1,
        input_tokens=900,
        output_tokens=120,
        ai_provider="openai",
        ai_model="model-1",
    )
    local_date = date(2026, 9, 4)

    async with database.transaction() as session:
        analysis = await AnalysisRepository(session).upsert_analysis(
            message_id=first_id,
            input_hash="hash-1",
            provider="openai",
            model="model-1",
            prompt_version="prompt-1",
            schema_version="2",
            analysis=make_analysis(),
        )
        digest = await DigestRepository(session).save_digest(
            account_id=account_id,
            local_date=local_date,
            timezone_name="America/Toronto",
            status=DigestStatus.PARTIAL,
            items=[
                (first_id, analysis.id, 0, DigestSection.DEADLINES),
                (second_id, None, 1, DigestSection.HIGHLIGHTS),
            ],
            coverage=coverage,
        )
        digest_id = digest.id

    async with database.session() as session:
        repo = DigestRepository(session)
        row = await repo.get_by_id(digest_id)
        assert row is not None
        restored = DigestRepository.to_domain(row, await repo.get_digest_items(digest_id), "ms-1")

    assert restored.coverage == coverage
    analyzed_item, preview_item = restored.items
    assert analyzed_item.deadline_text == "Friday 5 PM"
    assert analyzed_item.deadline_precision is DeadlinePrecision.DATETIME
    assert analyzed_item.deadline_date == local_date
    assert analyzed_item.deadline_at_utc == datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    assert analyzed_item.evidence == "Please approve the attached proposal by Friday."
    assert preview_item.deadline_precision is DeadlinePrecision.NONE
    assert preview_item.evidence is None

    async with database.transaction() as session:
        await DigestRepository(session).save_digest(
            account_id=account_id,
            local_date=local_date,
            timezone_name="America/Toronto",
            status=DigestStatus.EMPTY,
        )

    async with database.session() as session:
        row = await DigestRepository(session).get_by_id(digest_id)
        assert row is not None
        assert row.shortlisted_count is None
        assert DigestRepository.to_domain(row, [], "ms-1").coverage is None


@pytest.mark.asyncio
async def test_consent_grant_revoke_and_reactivate(database: Database) -> None:
    account_id, _ = await _account_and_messages(database)
    granted_at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    revoked_at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    regranted_at = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    async with database.transaction() as session:
        repo = ConsentRepository(session)
        consent_id = (await repo.grant(account_id, "openai", "disclosure-1", granted_at)).id
        active = await repo.get_active(account_id, "openai", "disclosure-1")
        assert active is not None
        assert active.id == consent_id
        assert active.granted_at_utc == granted_at
        assert await repo.get_active(account_id, "openai", "disclosure-2") is None

        assert await repo.revoke_all(account_id, "openai", revoked_at) == 1
        assert await repo.get_active(account_id, "openai", "disclosure-1") is None
        assert await repo.revoke_all(account_id, "openai", revoked_at) == 0

        again = await repo.grant(account_id, "openai", "disclosure-1", regranted_at)
        assert again.id == consent_id
        assert again.revoked_at_utc is None
        assert again.granted_at_utc == regranted_at

    async with database.session() as session:
        active = await ConsentRepository(session).get_active(account_id, "openai", "disclosure-1")
        assert active is not None
        assert active.granted_at_utc == regranted_at
