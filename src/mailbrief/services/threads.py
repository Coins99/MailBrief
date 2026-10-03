"""Track the threads of open actions by reading their metadata only (ADR 0015).

The threads of live, open actions in the connected account are read with a thread reader,
at most MAX_TRACKED_THREADS per run, most urgent action first. Only messages received after
the action's latest source in that thread are cached, the newest MAX_THREAD_MESSAGES per
thread, as ordinary metadata. Nothing here downloads a body, lists a folder or label, or
changes an action: thread activity is derived from the cached messages. Logs carry counts
only.

Replies the day's Inbox sync can't see (archived, or received on an earlier day) are found
in that cache by outside_replies(), at most MAX_OUTSIDE_REPLIES a run (ADR 0016).
"""

import asyncio
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import ActionStatus
from mailbrief.domain.analysis import ANALYSIS_SCHEMA_VERSION, DeadlinePrecision, deadline_due_at
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.messages import (
    NormalizedMessage,
    ProviderKind,
    is_own_message,
)
from mailbrief.domain.messages import own_addresses as _own_addresses
from mailbrief.domain.preferences import sender_excluded
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    MessageUnavailableError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
)
from mailbrief.ports.threads import ThreadReader, ThreadSnapshot
from mailbrief.services.actions import urgency
from mailbrief.services.calendar import DayWindow
from mailbrief.services.sync import sanitize_error_code
from mailbrief.storage.repositories import MessageRepository
from mailbrief.storage.tables import (
    AccountTable,
    ActionSourceTable,
    ActionTable,
    AnalysisTable,
    MessageTable,
)

logger = logging.getLogger(__name__)

MAX_TRACKED_THREADS: Final = 25
MAX_THREAD_MESSAGES: Final = 20
MAX_OUTSIDE_REPLIES: Final = 3
THREAD_CONCURRENCY: Final = 3
# These stop the whole check: every later request would fail the same way.
_STOPPING: Final = (AuthenticationRequiredError, ProviderPermissionError, ProviderRateLimitError)


@dataclass(frozen=True, slots=True)
class TrackedThread:
    """A thread to check, and the time after which its messages are new."""

    thread_id: str
    since_utc: datetime


@dataclass(frozen=True, slots=True)
class ThreadCheck:
    """What one check did: threads tracked, read, failed or gone, messages stored, and
    cached messages removed because Gmail has them in Trash or Spam. ``read_ids`` are the
    threads read successfully: the only ones outside replies may come from in this run.

    ``stopped_code`` names why the check stopped early, if it did.
    """

    tracked: int = 0
    checked: int = 0
    failed: int = 0
    gone: int = 0
    stored: int = 0
    removed: int = 0
    stopped_code: str | None = None
    read_ids: frozenset[str] = frozenset()


def own_addresses(account: AccountTable) -> frozenset[str]:
    """The account's own addresses, casefolded: the ones that tell the owner's mail apart."""
    return _own_addresses(account.email_address, account.account_addresses)


def _new_messages(
    messages: tuple[NormalizedMessage, ...], since_utc: datetime
) -> list[NormalizedMessage]:
    """Messages received strictly after ``since_utc``, the newest MAX_THREAD_MESSAGES."""
    later = sorted(
        (message for message in messages if message.received_at_utc > since_utc),
        key=lambda message: (message.received_at_utc, message.provider_message_id),
    )
    return later[-MAX_THREAD_MESSAGES:]


class ThreadService:
    """Reads tracked threads for one account and caches their later messages."""

    def __init__(self, session: AsyncSession, reader: ThreadReader) -> None:
        self._session = session
        self._reader = reader
        self._messages = MessageRepository(session)

    async def tracked(
        self, account: AccountTable, limit: int | None = MAX_TRACKED_THREADS
    ) -> list[TrackedThread]:
        """The threads to check for this account, most urgent action first, at most
        ``limit`` (MAX_TRACKED_THREADS by default; None for all of them, as ranking uses).

        A thread is tracked when a source of a live, open action has a snapshot of this
        account's provider and provider account ID. Its baseline is the earliest, across
        the actions tracking it, of each action's latest own source in that thread.
        """
        result = await self._session.execute(
            select(
                ActionTable,
                ActionSourceTable.provider_thread_id,
                ActionSourceTable.received_at_utc,
            )
            .join(ActionSourceTable, ActionSourceTable.action_id == ActionTable.id)
            .where(
                ActionTable.deleted_at_utc.is_(None),
                ActionTable.status == ActionStatus.OPEN.value,
                ActionSourceTable.provider == account.provider,
                ActionSourceTable.provider_account_id == account.provider_account_id,
                ActionSourceTable.provider_thread_id.is_not(None),
            )
        )
        baselines: dict[str, dict[int, datetime]] = {}
        order: dict[int, tuple[object, ...]] = {}
        for action, thread_id, received in result.tuples():
            assert thread_id is not None
            latest = baselines.setdefault(thread_id, {})
            latest[action.id] = max(latest.get(action.id, received), received)
            due = deadline_due_at(
                DeadlinePrecision(action.deadline_precision),
                action.deadline_date,
                action.deadline_at_utc,
                action.deadline_timezone,
            )
            order[action.id] = (*urgency(action.target_date, due, action.created_at_utc), action.id)
        threads = sorted(
            baselines,
            key=lambda thread_id: (min(order[key] for key in baselines[thread_id]), thread_id),
        )
        return [
            TrackedThread(thread_id=thread_id, since_utc=min(baselines[thread_id].values()))
            for thread_id in (threads if limit is None else threads[:limit])
        ]

    async def outside_replies(
        self,
        account: AccountTable,
        window: DayWindow,
        *,
        limit: int = MAX_OUTSIDE_REPLIES,
        excluded_senders: Sequence[str] = (),
        tracked: Sequence[TrackedThread] | None = None,
        read_threads: Collection[str],
    ) -> list[NormalizedMessage]:
        """Cached replies in tracked threads that ``window``'s Inbox sync can't see, newest
        first, at most ``limit``; replies the owner declined in a review (ADR 0017) come after
        the others, so they fill a place only when no other reply wants it.

        Only threads in ``read_threads``, the ones this run's check read (ThreadCheck.
        read_ids), are drawn on: a thread the check didn't read may hold a reply since
        trashed or marked spam, which only reading it reveals. None read means none offered.

        A reply qualifies when it was received after its thread's baseline (TrackedThread),
        isn't the owner's own (is_own_message), isn't from a sender in ``excluded_senders``,
        hasn't been analyzed at the current ANALYSIS_SCHEMA_VERSION, and isn't both inside
        ``window`` and in the Inbox, which that day's sync already covers. Two queries however
        many messages there are (one, when ``tracked`` is given); nothing is written. A
        caller that has already listed every tracked thread (``tracked(account,
        limit=None)``) passes it in to save that query.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        read = set(read_threads)
        if not read:
            return []
        every = await self.tracked(account, limit=None) if tracked is None else tracked
        since = {
            thread.thread_id: normalize_utc(thread.since_utc)
            for thread in every
            if thread.thread_id in read
        }
        if not since:
            return []
        analyzed = (
            exists()
            .where(
                AnalysisTable.message_id == MessageTable.id,
                AnalysisTable.schema_version == ANALYSIS_SCHEMA_VERSION,
            )
            .correlate(MessageTable)
        )
        covered = and_(
            MessageTable.is_in_inbox.is_(True),
            MessageTable.received_at_utc >= normalize_utc(window.start_utc),
            MessageTable.received_at_utc < normalize_utc(window.end_utc),
        )
        result = await self._session.scalars(
            select(MessageTable)
            .where(
                MessageTable.account_id == account.id,
                MessageTable.conversation_id.in_(sorted(since)),
                MessageTable.received_at_utc > min(since.values()),
                ~analyzed,
                ~covered,
            )
            .execution_options(populate_existing=True)
        )
        mine = own_addresses(account)
        provider = ProviderKind(account.provider)
        found: list[tuple[bool, NormalizedMessage]] = []
        for row in result:
            baseline = since.get(row.conversation_id or "")
            if baseline is None or normalize_utc(row.received_at_utc) <= baseline:
                continue
            message = MessageRepository.to_domain(row, account.provider_account_id, provider)
            if is_own_message(message, mine) or sender_excluded(
                message.sender.address, excluded_senders
            ):
                continue
            found.append((row.review_declined_at_utc is not None, message))
        found.sort(
            key=lambda item: (
                item[0],
                -item[1].received_at_utc.timestamp(),
                item[1].provider_message_id,
            )
        )
        return [message for _, message in found[:limit]]

    async def check(
        self, account: AccountTable, *, cancel: asyncio.Event | None = None
    ) -> ThreadCheck:
        """Read the tracked threads, at most THREAD_CONCURRENCY at a time, and cache their
        later messages, committing per thread.

        A cached message the thread now has in Trash or Spam is deleted from this account's
        cache, with its analyses, suggestions and brief items; action sources, draft sources
        and proposals keep their snapshots. It is cached again if it leaves Trash or Spam.

        A gone thread is counted and skipped, and another provider error counts as failed.
        Sign-in, permission and rate-limit errors stop the check; what was stored stays.
        A cancel stops before the next read. Pending reads never outlive the call.
        """
        tracked = await self.tracked(account)
        slots = asyncio.Semaphore(THREAD_CONCURRENCY)
        stop = asyncio.Event()

        async def read(
            thread: TrackedThread,
        ) -> tuple[TrackedThread, ThreadSnapshot | ProviderError | None]:
            async with slots:
                if stop.is_set() or (cancel is not None and cancel.is_set()):
                    return thread, None
                try:
                    return thread, await self._reader.fetch_thread(thread.thread_id)
                except ProviderError as exc:
                    if isinstance(exc, _STOPPING):
                        stop.set()  # Reads still waiting for a slot are skipped at once.
                    return thread, exc

        checked = failed = gone = stored = removed = 0
        read_ids: set[str] = set()
        stopped: str | None = None
        tasks = [asyncio.create_task(read(thread)) for thread in tracked]
        try:
            for finished in asyncio.as_completed(tasks):
                thread, result = await finished
                if result is None:
                    continue
                if isinstance(result, MessageUnavailableError):
                    gone += 1
                elif isinstance(result, _STOPPING):
                    stopped = sanitize_error_code(result)
                    stop.set()
                    break
                elif isinstance(result, ProviderError):
                    failed += 1
                else:
                    later = _new_messages(result.messages, thread.since_utc)
                    if later:
                        await self._messages.upsert_messages(account.id, later)
                    discarded = await self._messages.get_by_provider_ids(
                        account.id, result.discarded_ids
                    )
                    for row in discarded:
                        await self._session.delete(row)
                    if later or discarded:
                        await self._session.commit()
                    stored += len(later)
                    removed += len(discarded)
                    read_ids.add(thread.thread_id)
                    checked += 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "Thread check: %d tracked, %d checked, %d failed, %d gone, %d messages stored, "
            "%d removed%s",
            len(tracked),
            checked,
            failed,
            gone,
            stored,
            removed,
            f", stopped: {stopped}" if stopped else "",
        )
        return ThreadCheck(
            tracked=len(tracked),
            checked=checked,
            failed=failed,
            gone=gone,
            stored=stored,
            removed=removed,
            stopped_code=stopped,
            read_ids=frozenset(read_ids),
        )
