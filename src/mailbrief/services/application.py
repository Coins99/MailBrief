"""Top-level application workflow coordinating synchronization, ranking, and shortlisting."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from mailbrief.domain.common import normalize_utc
from mailbrief.domain.digests import SyncProgress, SyncResult, SyncStage, SyncStatus
from mailbrief.domain.messages import NormalizedMessage, ProviderKind, RankedMessage
from mailbrief.domain.preferences import sender_excluded
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.services.calendar import day_window, local_day_window, resolve_timezone
from mailbrief.services.ranking import (
    MAX_SHORTLIST_SIZE,
    ExcludedSenderError,
    ShortlistGate,
    ShortlistReviewError,
    rank_messages,
    review_shortlist,
)
from mailbrief.services.sync import SyncService
from mailbrief.services.threads import ThreadService
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import AccountTable

logger = logging.getLogger(__name__)

_COUNT_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def blocked_ids(messages: Sequence[NormalizedMessage], rules: Sequence[str]) -> frozenset[str]:
    """The IDs of messages whose sender matches one of the owner's sender rules."""
    return frozenset(
        message.provider_message_id
        for message in messages
        if sender_excluded(message.sender.address, rules)
    )


class ApplicationService:
    """Orchestrates account restoration, mailbox synchronization, and local ranking."""

    def __init__(
        self,
        provider: EmailProvider,
        message_repo: MessageRepository,
        sync_run_repo: SyncRunRepository,
        account_repo: AccountRepository,
        sync_service: SyncService | None = None,
        threads: ThreadService | None = None,
    ) -> None:
        self._provider = provider
        self._message_repo = message_repo
        self._sync_run_repo = sync_run_repo
        self._account_repo = account_repo
        self._sync_service = sync_service or SyncService(
            provider=provider,
            message_repo=message_repo,
            sync_run_repo=sync_run_repo,
            account_repo=account_repo,
        )
        self._threads = threads
        self._session = getattr(message_repo, "_session", None)

    async def _check_threads(
        self,
        account: AccountTable,
        sync_result: SyncResult,
        progress: Callable[[SyncProgress], None] | None,
        cancel: asyncio.Event | None,
    ) -> SyncResult:
        """Check the threads of open actions after today's sync (ADR 0015).

        Its counts join the sync result; a failed or stopped check never changes the sync's
        status.
        """
        assert self._threads is not None
        if progress:
            try:
                progress(
                    SyncProgress(
                        stage=SyncStage.THREADS,
                        pages_fetched=sync_result.page_count,
                        messages_fetched=sync_result.message_count,
                    )
                )
            except Exception as exc:
                logger.warning("Progress callback raised an exception: %s", type(exc).__name__)
        try:
            check = await self._threads.check(account, cancel=cancel)
        except Exception as exc:
            logger.warning("Thread check failed: %s", type(exc).__name__)
            if self._session:
                # A rollback expires loaded rows; the account is used again below.
                await self._session.rollback()
                await self._session.refresh(account)
            return sync_result.model_copy(update={"threads_stopped_code": "THREAD_CHECK_FAILED"})
        return sync_result.model_copy(
            update={
                "threads_tracked": check.tracked,
                "threads_checked": check.checked + check.gone,
                "threads_failed": check.failed,
                "thread_messages": check.stored,
                "threads_stopped_code": check.stopped_code,
            }
        )

    async def _remember_declines(
        self, account_id: int, *, left_out: set[str], taken_back: frozenset[str], now: datetime
    ) -> None:
        """Remember the owner's declines, and forget those they took back (ADR 0017)."""
        if not left_out and not taken_back:
            return
        await self._message_repo.set_review_declined(account_id, left_out, now)
        await self._message_repo.set_review_declined(account_id, taken_back, None)
        if self._session:
            await self._session.commit()
        logger.info(
            "Review left out %d messages and took back %d earlier declines",
            len(left_out),
            len(taken_back),
        )

    async def get_or_restore_account(self) -> AccountTable:
        """Connect or restore an existing account session and persist identity."""
        identity = await self._provider.connect()
        account = await self._account_repo.upsert(identity)
        if self._session:
            await self._session.commit()
        return account

    async def prepare_daily_shortlist(
        self,
        *,
        tz_key: str | None = None,
        now_utc: datetime | None = None,
        progress: Callable[[SyncProgress], None] | None = None,
        cancel: asyncio.Event | None = None,
        include_ids: tuple[str, ...] = (),
        exclude_ids: tuple[str, ...] = (),
        shortlist_gate: ShortlistGate | None = None,
        shortlist_limit: int = MAX_SHORTLIST_SIZE,
        excluded_senders: tuple[str, ...] = (),
        local_date: date | None = None,
    ) -> tuple[SyncResult, list[RankedMessage]]:
        """Connect, sync and rank one day, optionally reviewing all metadata before body access.

        The day is today in ``tz_key``'s zone, or ``local_date`` when given; callers check
        that a past date is allowed (services.history.check_brief_date). Only a sync of the
        window containing now advances the account's last-sync time. Ranking always uses
        the real now.

        At most ``shortlist_limit`` messages are selected. A message whose sender matches
        ``excluded_senders`` is never selected: including it, here or in review, raises
        ExcludedSenderError before any body is read.

        After a complete or partial sync of today's window, the threads of open actions are
        checked when a thread service is composed; past days never check threads.

        Today's run with a thread service also brings in up to MAX_OUTSIDE_REPLIES cached
        replies in tracked threads that today's Inbox sync can't see (ThreadService.
        outside_replies, ADR 0016). They are ranked with the tracked-thread bonus, never come
        from a sender in ``excluded_senders``, and compete for the selection, the review and
        the limit like any other message; the result's ``outside_ids`` names the selected
        ones.

        A message the owner left out of an earlier review (``review_declined_at_utc``) is
        skipped by the automatic selection until they select it again (ADR 0017). Only an
        active choice is remembered: a message the automatic selection picked that the owner
        unchecks in the gate's review, or names in ``exclude_ids``, becomes declined. One that
        is merely pushed out by the limit, because ``include_ids`` named others, is not. Each
        message selected, by the gate or by ``include_ids``, has any earlier decline forgotten.

        Returns the terminal SyncResult and deterministic shortlisted RankedMessage items.
        """
        now = normalize_utc(now_utc) if now_utc is not None else datetime.now(UTC)
        account = await self.get_or_restore_account()

        tz = resolve_timezone(tz_key)
        window = local_day_window(now, tz) if local_date is None else day_window(local_date, tz)
        today = window.start_utc <= now < window.end_utc

        # 1. Sync messages from provider
        sync_result = await self._sync_service.sync_day(
            account_id=account.id,
            account_identity=account.email_address,
            window=window,
            progress=progress,
            cancel=cancel,
            record_last_sync=today,
        )

        if sync_result.status in {SyncStatus.CANCELLED, SyncStatus.FAILED}:
            return sync_result, []

        if today and self._threads is not None and not (cancel and cancel.is_set()):
            sync_result = await self._check_threads(account, sync_result, progress, cancel)

        if cancel and cancel.is_set():
            return (
                sync_result.model_copy(
                    update={
                        "status": SyncStatus.CANCELLED,
                        "error_code": None,
                        "shortlisted_message_keys": (),
                    }
                ),
                [],
            )

        # 2. Read back cached messages from database for the day window
        rows = await self._message_repo.get_messages_in_range(
            account.id,
            window.start_utc,
            window.end_utc,
            inbox_only=True,
        )
        provider_kind = ProviderKind(account.provider)
        domain_messages = [
            MessageRepository.to_domain(row, account.provider_account_id, provider_kind)
            for row in rows
        ]
        # Every tracked thread, for ranking and for the replies the Inbox sync can't see.
        tracked_threads = (
            await self._threads.tracked(account, limit=None)
            if self._threads is not None and (rows or today)
            else []
        )
        # Those replies join only today's run, and never come from blocked senders.
        outside = (
            [
                message
                for message in await self._threads.outside_replies(
                    account, window, excluded_senders=excluded_senders, tracked=tracked_threads
                )
                if not sender_excluded(message.sender.address, excluded_senders)
            ]
            if today and self._threads is not None
            else []
        )

        if not rows and not outside:
            review_shortlist(
                [], include_ids=include_ids, exclude_ids=exclude_ids, limit=shortlist_limit
            )
            return sync_result, []

        if progress:
            try:
                progress(
                    SyncProgress(
                        stage=SyncStage.RANKING,
                        pages_fetched=sync_result.page_count,
                        messages_fetched=sync_result.message_count,
                    )
                )
            except Exception as exc:
                logger.warning("Progress callback raised an exception during ranking: %s", exc)

        # 3. Deterministic local ranking; later messages in the threads of open actions
        # rank higher (ADR 0016).
        user_addresses = (
            account.account_addresses
            if getattr(account, "account_addresses", None)
            else (account.email_address,)
        )  # noqa: E501
        tracked = {thread.thread_id: thread.since_utc for thread in tracked_threads}
        ranked = rank_messages(
            [*domain_messages, *outside],
            user_email=user_addresses,  # type: ignore[arg-type]
            now_utc=now,
            tracked=tracked,
        )

        # 4. Batch persist computed rankings in SQLite
        rankings_data = [
            (row.id, r.score, r.reasons) for row, r in zip(rows, ranked[: len(rows)], strict=True)
        ]
        await self._message_repo.update_rankings(rankings_data)
        await self._message_repo.update_rankings_by_provider_id(
            account.id,
            [(r.message.provider_message_id, r.score, r.reasons) for r in ranked[len(rows) :]],
        )
        if self._session:
            await self._session.commit()

        # 5. Deterministic shortlist selection, never from excluded senders
        blocked = blocked_ids(domain_messages, excluded_senders)
        outside_all = frozenset(message.provider_message_id for message in outside)
        logger.info(
            "Shortlist limit %d; %d sender rules block %d messages; %d outside replies",
            shortlist_limit,
            len(excluded_senders),
            len(blocked),
            len(outside_all),
        )
        declined = frozenset(
            row.provider_message_id for row in rows if row.review_declined_at_utc is not None
        ) | await self._message_repo.declined_among(account.id, outside_all)
        shortlist = review_shortlist(
            ranked,
            include_ids=include_ids,
            exclude_ids=exclude_ids,
            limit=shortlist_limit,
            blocked=blocked,
            declined=declined,
        )
        # What the automatic selection alone picks, to tell what the owner then left out.
        automatic = (
            review_shortlist(ranked, limit=shortlist_limit, blocked=blocked, declined=declined)
            if include_ids or exclude_ids
            else shortlist
        )
        if shortlist_gate is not None:
            candidates = tuple(
                sorted(
                    ranked,
                    key=lambda item: (
                        -item.score,
                        -item.message.received_at_utc.timestamp(),
                        item.message.provider_message_id,
                    ),
                )
            )
            selected = await shortlist_gate.review(
                candidates,
                tuple(item.message.provider_message_id for item in shortlist),
                blocked_ids=blocked,
                outside_ids=outside_all,
                declined_ids=declined,
                limit=shortlist_limit,
            )
            if selected is None or (cancel is not None and cancel.is_set()):
                return sync_result.model_copy(update={"status": SyncStatus.CANCELLED}), []
            available = {item.message.provider_message_id for item in candidates}
            if (
                len(selected) > shortlist_limit
                or len(selected) != len(set(selected))
                or not set(selected) <= available
            ):
                raise ShortlistReviewError(
                    "Reviewed messages must be unique members of today's Inbox, up to "
                    f"{_COUNT_WORDS[shortlist_limit - 1]}."
                )
            if blocked & set(selected):
                raise ExcludedSenderError()
            shortlist = [
                item for item in candidates if item.message.provider_message_id in selected
            ]
        shortlist_keys = tuple(m.message.provider_message_id for m in shortlist)
        picked = {item.message.provider_message_id for item in automatic}
        await self._remember_declines(
            account.id,
            # The owner's review decides what was left out; without one, only --exclude does.
            left_out=(
                picked.difference(shortlist_keys)
                if shortlist_gate is not None
                else picked.intersection(exclude_ids)
            ),
            taken_back=declined.intersection(shortlist_keys),
            now=now,
        )

        return sync_result.model_copy(
            update={
                "shortlisted_message_keys": shortlist_keys,
                "outside_ids": outside_all.intersection(shortlist_keys),
            }
        ), shortlist
