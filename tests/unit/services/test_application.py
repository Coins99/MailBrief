"""The thread check after today's sync: when it runs, what it reports, and that it never
fails the sync."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.digests import SyncProgress, SyncStage, SyncStatus
from mailbrief.domain.messages import RankReason
from mailbrief.services.application import ApplicationService
from mailbrief.services.threads import (
    MAX_TRACKED_THREADS,
    ThreadCheck,
    ThreadService,
    TrackedThread,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import AccountTable
from tests.factories import make_message
from tests.unit.services.test_sync import FakeEmailProvider

NOW = datetime(2026, 9, 30, 16, tzinfo=UTC)
ZONE = "America/Toronto"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


class RecordingThreads(ThreadService):
    """Records each check and answers with a fixed result, or raises."""

    def __init__(self, session: AsyncSession, result: ThreadCheck | Exception) -> None:
        super().__init__(session, reader=None)  # type: ignore[arg-type]
        self.result = result
        self.checked: list[str] = []

    async def check(
        self, account: AccountTable, *, cancel: asyncio.Event | None = None
    ) -> ThreadCheck:
        self.checked.append(account.email_address)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def application(
    session: AsyncSession, threads: ThreadService, email: FakeEmailProvider | None = None
) -> ApplicationService:
    return ApplicationService(
        provider=email
        or FakeEmailProvider(pages=[[make_message(provider_message_id="m1", received_at_utc=NOW)]]),
        message_repo=MessageRepository(session),
        sync_run_repo=SyncRunRepository(session),
        account_repo=AccountRepository(session),
        threads=threads,
    )


async def test_today_s_sync_checks_threads_and_reports_the_counts(session: AsyncSession) -> None:
    threads = RecordingThreads(
        session,
        ThreadCheck(tracked=4, checked=2, failed=1, gone=1, stored=3, stopped_code=None),
    )
    stages: list[SyncStage] = []

    result, shortlist = await application(session, threads).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, progress=lambda item: stages.append(item.stage)
    )

    assert threads.checked == ["user@example.com"]
    assert SyncStage.THREADS in stages
    assert stages.index(SyncStage.THREADS) < stages.index(SyncStage.RANKING)
    assert result.status is SyncStatus.COMPLETE
    assert (
        result.threads_tracked,
        result.threads_checked,
        result.threads_failed,
        result.thread_messages,
        result.threads_stopped_code,
    ) == (4, 3, 1, 3, None)
    assert [item.message.provider_message_id for item in shortlist] == ["m1"]


async def test_a_stopped_check_is_reported_without_changing_the_sync(
    session: AsyncSession,
) -> None:
    threads = RecordingThreads(session, ThreadCheck(tracked=2, stopped_code="RATE_LIMITED"))

    result, _ = await application(session, threads).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW
    )

    assert result.status is SyncStatus.COMPLETE
    assert result.threads_stopped_code == "RATE_LIMITED"
    assert result.error_code is None


async def test_an_unexpected_failure_never_fails_the_sync(session: AsyncSession) -> None:
    threads = RecordingThreads(session, RuntimeError("database locked"))

    result, shortlist = await application(session, threads).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW
    )

    assert result.status is SyncStatus.COMPLETE
    assert result.threads_stopped_code == "THREAD_CHECK_FAILED"
    assert [item.message.provider_message_id for item in shortlist] == ["m1"]


async def test_a_past_day_never_checks_threads(session: AsyncSession) -> None:
    threads = RecordingThreads(session, ThreadCheck(tracked=1))

    result, _ = await application(session, threads).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, local_date=date(2026, 9, 29)
    )

    assert threads.checked == []
    assert result.threads_tracked == 0


async def test_a_failed_sync_never_checks_threads(session: AsyncSession) -> None:
    email = FakeEmailProvider(pages=[[make_message(received_at_utc=NOW)]])
    email.fail_at_page = 1
    threads = RecordingThreads(session, ThreadCheck(tracked=1))

    result, _ = await application(session, threads, email).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW
    )

    assert result.status is SyncStatus.FAILED
    assert threads.checked == []


async def test_a_cancelled_sync_never_checks_threads(session: AsyncSession) -> None:
    threads = RecordingThreads(session, ThreadCheck(tracked=1))
    cancel = asyncio.Event()
    cancel.set()

    result, shortlist = await application(session, threads).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, cancel=cancel
    )

    assert result.status is SyncStatus.CANCELLED
    assert threads.checked == [] and shortlist == []


async def test_a_cancel_during_the_check_cancels_the_run(session: AsyncSession) -> None:
    cancel = asyncio.Event()

    class CancellingThreads(RecordingThreads):
        async def check(
            self, account: AccountTable, *, cancel: asyncio.Event | None = None
        ) -> ThreadCheck:
            assert cancel is not None
            cancel.set()
            return ThreadCheck(tracked=3, checked=1)

    result, shortlist = await application(
        session, CancellingThreads(session, ThreadCheck())
    ).prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW, cancel=cancel)

    assert result.status is SyncStatus.CANCELLED
    assert result.threads_checked == 1  # What was checked is still reported.
    assert shortlist == []


async def test_a_broken_progress_callback_doesn_t_stop_the_check(
    session: AsyncSession,
) -> None:
    threads = RecordingThreads(session, ThreadCheck(tracked=1, checked=1))

    def broken(progress: SyncProgress) -> None:
        if progress.stage is SyncStage.THREADS:
            raise RuntimeError("the window closed")

    result, _ = await application(session, threads).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, progress=broken
    )

    assert threads.checked and result.threads_checked == 1


class FixedTracking(RecordingThreads):
    """Tracks fixed threads and records the limit ranking asks for."""

    def __init__(self, session: AsyncSession, threads: list[TrackedThread]) -> None:
        super().__init__(session, ThreadCheck())
        self.threads = threads
        self.limits: list[int | None] = []

    async def tracked(
        self, account: AccountTable, limit: int | None = MAX_TRACKED_THREADS
    ) -> list[TrackedThread]:
        self.limits.append(limit)
        return self.threads


async def test_a_later_reply_in_a_tracked_thread_ranks_higher(session: AsyncSession) -> None:
    email = FakeEmailProvider(
        pages=[
            [
                make_message(
                    provider_message_id="reply", conversation_id="deck", received_at_utc=NOW
                ),
                make_message(
                    provider_message_id="other", conversation_id="misc", received_at_utc=NOW
                ),
            ]
        ]
    )
    threads = FixedTracking(session, [TrackedThread("deck", NOW - timedelta(days=1))])

    _, shortlist = await application(session, threads, email).prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW
    )

    ranked = {item.message.provider_message_id: item for item in shortlist}
    assert ranked["reply"].score == ranked["other"].score + 20
    assert RankReason.TRACKED_THREAD_REPLY in ranked["reply"].reasons
    assert RankReason.TRACKED_THREAD_REPLY not in ranked["other"].reasons
    assert threads.limits == [None]  # Every tracked thread, not only those checked.
