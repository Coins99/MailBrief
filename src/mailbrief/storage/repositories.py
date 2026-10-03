"""Database repositories implementing transactional persistence and domain mappings."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import cast

from pydantic import HttpUrl
from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import SuggestionView
from mailbrief.domain.analysis import (
    SUMMARY_MAX_CHARS,
    AnalysisCategory,
    DeadlinePrecision,
    FollowUpKind,
    MessageAnalysis,
)
from mailbrief.domain.briefs import AUTO_SEND_LIMIT_MAX
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.digests import (
    DailyDigest,
    DigestCoverage,
    DigestItem,
    DigestSection,
    DigestStatus,
    SavedBriefSummary,
    SyncStatus,
)
from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    MessageImportance,
    NormalizedMessage,
    ProviderKind,
    RankReason,
)
from mailbrief.storage.actions import suggestion_from_row, suggestion_views
from mailbrief.storage.database import MAX_SQLITE_BATCH_SIZE
from mailbrief.storage.tables import (
    AccountTable,
    ActionSuggestionTable,
    AIConsentTable,
    AnalysisTable,
    DigestItemTable,
    DigestTable,
    MessageTable,
    OwnerConsentTable,
    SyncRunTable,
)
from mailbrief.text.prepare import truncate_at_boundary

_NO_SUMMARY = "No summary available"


class AccountRepository:
    """Repository managing connected provider accounts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, identity: AccountIdentity) -> AccountTable:
        """Insert or update an account identity idempotently."""
        provider_val = (
            identity.provider.value
            if isinstance(identity.provider, ProviderKind)
            else str(identity.provider)
        )
        base_stmt = sqlite_insert(AccountTable).values(
            provider=provider_val,
            provider_account_id=identity.provider_account_id,
            email_address=identity.email_address,
            display_name=identity.display_name,
            tenant_id=identity.tenant_id,
            account_addresses=list(identity.account_addresses)
            if identity.account_addresses
            else [identity.email_address],  # noqa: E501
            created_at_utc=datetime.now(UTC),
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=["provider", "provider_account_id"],
            set_={
                "email_address": base_stmt.excluded.email_address,
                "display_name": base_stmt.excluded.display_name,
                "tenant_id": base_stmt.excluded.tenant_id,
                "account_addresses": base_stmt.excluded.account_addresses,
            },
        ).returning(AccountTable)
        account = await self._session.scalar(stmt)
        assert account is not None
        return account

    @staticmethod
    def to_domain(account: AccountTable) -> AccountIdentity:
        """Convert an AccountTable ORM model into a domain AccountIdentity."""
        return AccountIdentity(
            provider=ProviderKind(account.provider),
            provider_account_id=account.provider_account_id,
            email_address=account.email_address,
            display_name=account.display_name,
            tenant_id=account.tenant_id,
            account_addresses=tuple(account.account_addresses or (account.email_address,)),
        )

    async def get_by_id(self, account_id: int) -> AccountTable | None:
        """Fetch an account by its primary key ID."""
        return await self._session.get(AccountTable, account_id)

    async def get_by_provider_identity(
        self,
        provider: str | ProviderKind,
        provider_account_id: str,
    ) -> AccountTable | None:
        """Fetch an account by its unique provider identity."""
        provider_val = provider.value if isinstance(provider, ProviderKind) else str(provider)
        stmt = select(AccountTable).where(
            AccountTable.provider == provider_val,
            AccountTable.provider_account_id == provider_account_id,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_email(self, email_address: str) -> AccountTable | None:
        """Fetch an account by its email address."""
        stmt = select(AccountTable).where(AccountTable.email_address == email_address)
        result = await self._session.scalars(stmt)
        return result.first()

    async def list_all(self) -> list[AccountTable]:
        """Return all persisted accounts ordered by ID."""
        stmt = select(AccountTable).order_by(AccountTable.id.asc())
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def count(self) -> int:
        """Return the total number of connected accounts."""
        count_val = await self._session.scalar(select(func.count()).select_from(AccountTable))
        return count_val or 0

    async def update_last_sync(self, account_id: int, last_sync_at_utc: datetime) -> None:
        """Update the last synchronization timestamp for an account."""
        stmt = (
            update(AccountTable)
            .where(AccountTable.id == account_id)
            .values(last_sync_at_utc=normalize_utc(last_sync_at_utc))
        )
        await self._session.execute(stmt)

    async def delete_by_id(self, account_id: int) -> bool:
        """Delete an account and its cascade records by ID."""
        account = await self._session.get(AccountTable, account_id)
        if account is None:
            return False
        await self._session.delete(account)
        return True


class MessageRepository:
    """Repository managing normalized message metadata and ranking scores."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_messages(
        self,
        account_id: int,
        messages: Sequence[NormalizedMessage],
    ) -> list[MessageTable]:
        """Idempotently insert or update normalized message metadata in bulk.

        Deduplicates within the provided batch to protect against SQLite single-statement
        re-modification constraints and chunks queries into safe parameter sizes.
        """
        if not messages:
            return []

        # Deduplicate within batch preserving the latest item
        deduped: dict[str, NormalizedMessage] = {}
        for m in messages:
            deduped[m.provider_message_id] = m
        unique_messages = list(deduped.values())

        saved_rows: list[MessageTable] = []
        for i in range(0, len(unique_messages), MAX_SQLITE_BATCH_SIZE):
            batch = unique_messages[i : i + MAX_SQLITE_BATCH_SIZE]
            rows = [
                {
                    "account_id": account_id,
                    "provider_message_id": m.provider_message_id,
                    "internet_message_id": m.internet_message_id,
                    "conversation_id": m.conversation_id,
                    "subject": m.subject,
                    "sender_name": m.sender.name,
                    "sender_address": m.sender.address,
                    "to_recipients_json": [
                        {"name": r.name, "address": r.address} for r in m.to_recipients
                    ],
                    "received_at_utc": m.received_at_utc,
                    "is_read": m.is_read,
                    "is_in_inbox": m.is_in_inbox,
                    "is_sent": m.is_sent,
                    "importance": (
                        m.importance.value
                        if isinstance(m.importance, MessageImportance)
                        else str(m.importance)
                    ),
                    "has_attachments": m.has_attachments,
                    "body_preview": m.body_preview,
                    "web_link": str(m.web_link),
                    "rank_reasons_json": [],
                    "synced_at_utc": datetime.now(UTC),
                }
                for m in batch
            ]

            base_stmt = sqlite_insert(MessageTable).values(rows)
            stmt = base_stmt.on_conflict_do_update(
                index_elements=["account_id", "provider_message_id"],
                set_={
                    "internet_message_id": base_stmt.excluded.internet_message_id,
                    "conversation_id": base_stmt.excluded.conversation_id,
                    "subject": base_stmt.excluded.subject,
                    "sender_name": base_stmt.excluded.sender_name,
                    "sender_address": base_stmt.excluded.sender_address,
                    "to_recipients_json": base_stmt.excluded.to_recipients_json,
                    "received_at_utc": base_stmt.excluded.received_at_utc,
                    "is_read": base_stmt.excluded.is_read,
                    "is_in_inbox": base_stmt.excluded.is_in_inbox,
                    "is_sent": base_stmt.excluded.is_sent,
                    "importance": base_stmt.excluded.importance,
                    "has_attachments": base_stmt.excluded.has_attachments,
                    "body_preview": base_stmt.excluded.body_preview,
                    "web_link": base_stmt.excluded.web_link,
                    "synced_at_utc": base_stmt.excluded.synced_at_utc,
                },
            ).returning(MessageTable)
            # Refresh rows already loaded in this session instead of returning them stale.
            stmt = stmt.execution_options(populate_existing=True)

            result = await self._session.scalars(stmt)
            saved_rows.extend(result.all())

        return saved_rows

    async def update_ranking(
        self,
        message_id: int,
        rank_score: int,
        rank_reasons: Sequence[str | RankReason],
    ) -> None:
        """Persist computed rank score and readable reasons for a message."""
        reasons_list = [r.value if isinstance(r, RankReason) else str(r) for r in rank_reasons]
        stmt = (
            update(MessageTable)
            .where(MessageTable.id == message_id)
            .values(rank_score=rank_score, rank_reasons_json=reasons_list)
        )
        await self._session.execute(stmt)

    async def update_rankings(
        self,
        rankings: Sequence[tuple[int, int, Sequence[str | RankReason]]],
    ) -> None:
        """Persist computed rank scores and readable reasons for multiple messages by ID."""
        if not rankings:
            return
        for message_id, rank_score, rank_reasons in rankings:
            reasons_list = [r.value if isinstance(r, RankReason) else str(r) for r in rank_reasons]
            stmt = (
                update(MessageTable)
                .where(MessageTable.id == message_id)
                .values(rank_score=rank_score, rank_reasons_json=reasons_list)
            )
            await self._session.execute(stmt)

    async def update_rankings_by_provider_id(
        self,
        account_id: int,
        rankings: Sequence[tuple[str, int, Sequence[str | RankReason]]],
    ) -> None:
        """Persist rank scores and reasons for multiple messages by provider_message_id."""
        if not rankings:
            return
        for provider_msg_id, rank_score, rank_reasons in rankings:
            reasons_list = [r.value if isinstance(r, RankReason) else str(r) for r in rank_reasons]
            stmt = (
                update(MessageTable)
                .where(
                    MessageTable.account_id == account_id,
                    MessageTable.provider_message_id == provider_msg_id,
                )
                .values(rank_score=rank_score, rank_reasons_json=reasons_list)
            )
            await self._session.execute(stmt)

    async def get_messages_in_range(
        self,
        account_id: int,
        start_utc: datetime,
        end_utc: datetime,
        *,
        inbox_only: bool = False,
    ) -> list[MessageTable]:
        """Fetch all messages for an account within a UTC datetime range."""
        stmt = (
            select(MessageTable)
            .where(
                MessageTable.account_id == account_id,
                MessageTable.received_at_utc >= normalize_utc(start_utc),
                MessageTable.received_at_utc < normalize_utc(end_utc),
            )
            .order_by(MessageTable.received_at_utc.desc(), MessageTable.id.asc())
        )
        if inbox_only:
            stmt = stmt.where(MessageTable.is_in_inbox.is_(True))
        result = await self._session.scalars(stmt.execution_options(populate_existing=True))
        return list(result.all())

    async def get_cached_page(
        self,
        account_id: int,
        start_utc: datetime,
        end_utc: datetime,
        *,
        offset: int = 0,
        page_size: int = 100,
    ) -> tuple[list[MessageTable], bool]:
        """Bounded offline reads, including messages no longer in the cached Inbox."""
        if offset < 0 or not 1 <= page_size <= 200:
            raise ValueError("Invalid cached-mail page bounds.")
        result = await self._session.scalars(
            select(MessageTable)
            .where(
                MessageTable.account_id == account_id,
                MessageTable.received_at_utc >= normalize_utc(start_utc),
                MessageTable.received_at_utc < normalize_utc(end_utc),
            )
            .order_by(MessageTable.received_at_utc.desc(), MessageTable.id.asc())
            .offset(offset)
            .limit(page_size + 1)
        )
        rows = list(result.all())
        return rows[:page_size], len(rows) > page_size

    async def reconcile_inbox(
        self, account_id: int, start_utc: datetime, end_utc: datetime, seen_ids: set[str]
    ) -> None:
        """Call only after complete enumeration; preserve cached content and references."""
        window = (
            MessageTable.account_id == account_id,
            MessageTable.received_at_utc >= normalize_utc(start_utc),
            MessageTable.received_at_utc < normalize_utc(end_utc),
        )
        await self._session.execute(update(MessageTable).where(*window).values(is_in_inbox=False))
        identifiers = sorted(seen_ids)
        for offset in range(0, len(identifiers), MAX_SQLITE_BATCH_SIZE):
            await self._session.execute(
                update(MessageTable)
                .where(
                    *window,
                    MessageTable.provider_message_id.in_(
                        identifiers[offset : offset + MAX_SQLITE_BATCH_SIZE]
                    ),
                )
                .values(is_in_inbox=True)
            )

    async def get_by_provider_ids(
        self, account_id: int, provider_message_ids: Iterable[str]
    ) -> list[MessageTable]:
        """The cached messages among these IDs, in any order; IDs no longer cached are
        silently absent."""
        found: list[MessageTable] = []
        identifiers = sorted(set(provider_message_ids))
        for offset in range(0, len(identifiers), MAX_SQLITE_BATCH_SIZE):
            result = await self._session.scalars(
                select(MessageTable)
                .where(
                    MessageTable.account_id == account_id,
                    MessageTable.provider_message_id.in_(
                        identifiers[offset : offset + MAX_SQLITE_BATCH_SIZE]
                    ),
                )
                .execution_options(populate_existing=True)
            )
            found.extend(result)
        return found

    async def declined_among(
        self, account_id: int, provider_message_ids: Iterable[str]
    ) -> frozenset[str]:
        """The IDs among these that the owner declined in a review (ADR 0017)."""
        found: set[str] = set()
        identifiers = sorted(set(provider_message_ids))
        for offset in range(0, len(identifiers), MAX_SQLITE_BATCH_SIZE):
            result = await self._session.scalars(
                select(MessageTable.provider_message_id).where(
                    MessageTable.account_id == account_id,
                    MessageTable.provider_message_id.in_(
                        identifiers[offset : offset + MAX_SQLITE_BATCH_SIZE]
                    ),
                    MessageTable.review_declined_at_utc.is_not(None),
                )
            )
            found.update(result)
        return frozenset(found)

    async def set_review_declined(
        self, account_id: int, provider_message_ids: Iterable[str], now_utc: datetime | None
    ) -> int:
        """Remember that the owner declined these messages (``now_utc``), or forget it
        (None); returns how many rows changed.

        Rows change as ORM objects, never by a bulk UPDATE, so messages already loaded in
        the session show the change. The caller commits.
        """
        changed = 0
        identifiers = sorted(set(provider_message_ids))
        stamp = None if now_utc is None else normalize_utc(now_utc)
        for offset in range(0, len(identifiers), MAX_SQLITE_BATCH_SIZE):
            result = await self._session.scalars(
                select(MessageTable)
                .where(
                    MessageTable.account_id == account_id,
                    MessageTable.provider_message_id.in_(
                        identifiers[offset : offset + MAX_SQLITE_BATCH_SIZE]
                    ),
                )
                .execution_options(populate_existing=True)
            )
            for row in result:
                if (row.review_declined_at_utc is None) != (stamp is None):
                    row.review_declined_at_utc = stamp
                    changed += 1
        await self._session.flush()
        return changed

    async def get_by_provider_message_id(
        self,
        account_id: int,
        provider_message_id: str,
    ) -> MessageTable | None:
        """Fetch a single message by its provider message ID."""
        stmt = select(MessageTable).where(
            MessageTable.account_id == account_id,
            MessageTable.provider_message_id == provider_message_id,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_id(self, message_id: int) -> MessageTable | None:
        """Fetch a single message by its internal primary key ID."""
        return await self._session.get(MessageTable, message_id)

    @staticmethod
    def to_domain(
        row: MessageTable,
        provider_account_id: str,
        provider: ProviderKind,
    ) -> NormalizedMessage:
        """Map a MessageTable ORM entity to a NormalizedMessage domain model."""
        return NormalizedMessage(
            provider=provider,
            provider_account_id=provider_account_id,
            provider_message_id=row.provider_message_id,
            internet_message_id=row.internet_message_id,
            conversation_id=row.conversation_id,
            subject=row.subject,
            sender=EmailContact(name=row.sender_name, address=row.sender_address),
            to_recipients=tuple(
                EmailContact(name=r.get("name"), address=str(r.get("address", "")))
                for r in row.to_recipients_json
            ),
            received_at_utc=row.received_at_utc,
            is_read=row.is_read,
            is_in_inbox=row.is_in_inbox,
            is_sent=row.is_sent,
            importance=MessageImportance(row.importance),
            has_attachments=row.has_attachments,
            body_preview=row.body_preview,
            web_link=HttpUrl(row.web_link),
        )


class AnalysisRepository:
    """Repository managing structured AI analysis results and caching."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_analysis(
        self,
        message_id: int,
        input_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        analysis: MessageAnalysis,
    ) -> AnalysisTable:
        """Idempotently insert or update a structured analysis result and its suggestions.

        The analysis's stored suggestions are replaced by ``analysis.suggestions``, so an
        analysis without suggestions ends up with none.
        """
        category_val = (
            analysis.category.value
            if isinstance(analysis.category, AnalysisCategory)
            else str(analysis.category)
        )
        base_stmt = sqlite_insert(AnalysisTable).values(
            message_id=message_id,
            input_hash=input_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            category=category_val,
            summary=analysis.summary,
            action_required=analysis.action_required,
            action_text=analysis.action_text,
            deadline_text=analysis.deadline_text,
            deadline_at_utc=analysis.deadline_at_utc,
            deadline_precision=analysis.deadline_precision.value,
            deadline_date=analysis.deadline_date,
            deadline_timezone=analysis.deadline_timezone,
            confidence=analysis.confidence,
            evidence=analysis.evidence,
            analyzed_at_utc=datetime.now(UTC),
            follow_up_kind=analysis.follow_up.value,
            follow_up_evidence=analysis.follow_up_evidence,
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[
                "message_id",
                "input_hash",
                "provider",
                "model",
                "prompt_version",
                "schema_version",
            ],
            set_={
                "category": base_stmt.excluded.category,
                "summary": base_stmt.excluded.summary,
                "action_required": base_stmt.excluded.action_required,
                "action_text": base_stmt.excluded.action_text,
                "deadline_text": base_stmt.excluded.deadline_text,
                "deadline_at_utc": base_stmt.excluded.deadline_at_utc,
                "deadline_precision": base_stmt.excluded.deadline_precision,
                "deadline_date": base_stmt.excluded.deadline_date,
                "deadline_timezone": base_stmt.excluded.deadline_timezone,
                "confidence": base_stmt.excluded.confidence,
                "evidence": base_stmt.excluded.evidence,
                "analyzed_at_utc": base_stmt.excluded.analyzed_at_utc,
                "follow_up_kind": base_stmt.excluded.follow_up_kind,
                "follow_up_evidence": base_stmt.excluded.follow_up_evidence,
            },
        ).returning(AnalysisTable)
        result_table = await self._session.scalar(stmt)
        assert result_table is not None
        await self._session.execute(
            delete(ActionSuggestionTable).where(
                ActionSuggestionTable.analysis_id == result_table.id
            )
        )
        if analysis.suggestions:
            await self._session.execute(
                insert(ActionSuggestionTable).values(
                    [
                        {
                            "analysis_id": result_table.id,
                            "position": suggestion.position,
                            "title": suggestion.title,
                            "ownership": suggestion.ownership.value,
                            "effort": None
                            if suggestion.effort is None
                            else suggestion.effort.value,
                            "deadline_text": suggestion.deadline_text,
                            "deadline_precision": suggestion.deadline_precision.value,
                            "deadline_date": suggestion.deadline_date,
                            "deadline_at_utc": suggestion.deadline_at_utc,
                            "deadline_timezone": suggestion.deadline_timezone,
                            "suggested_target_date": suggestion.suggested_target_date,
                            "target_reason": (
                                None
                                if suggestion.target_reason is None
                                else suggestion.target_reason.value
                            ),
                            "steps_json": list(suggestion.steps),
                            "evidence": suggestion.evidence,
                            "fingerprint": suggestion.fingerprint,
                        }
                        for suggestion in analysis.suggestions
                    ]
                )
            )
        return result_table

    async def get_cached_analysis(
        self,
        message_id: int,
        input_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
    ) -> AnalysisTable | None:
        """Retrieve the cached analysis matching all six cache-identity columns."""
        stmt = select(AnalysisTable).where(
            AnalysisTable.message_id == message_id,
            AnalysisTable.input_hash == input_hash,
            AnalysisTable.provider == provider,
            AnalysisTable.model == model,
            AnalysisTable.prompt_version == prompt_version,
            AnalysisTable.schema_version == schema_version,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_message_id(self, message_id: int) -> list[AnalysisTable]:
        """Retrieve all analysis records associated with a message."""
        stmt = (
            select(AnalysisTable)
            .where(AnalysisTable.message_id == message_id)
            .order_by(AnalysisTable.analyzed_at_utc.desc())
        )
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def get_suggestions(self, analysis_id: int) -> list[ActionSuggestionTable]:
        """The analysis's stored suggestions in position order."""
        stmt = (
            select(ActionSuggestionTable)
            .where(ActionSuggestionTable.analysis_id == analysis_id)
            .order_by(ActionSuggestionTable.position.asc())
        )
        result = await self._session.scalars(stmt)
        return list(result.all())

    @staticmethod
    def to_domain(
        row: AnalysisTable,
        message_key: str,
        suggestions: Sequence[ActionSuggestionTable] = (),
    ) -> MessageAnalysis:
        """Map an AnalysisTable ORM entity and its suggestions to a MessageAnalysis.

        Rows saved before migration 0004 read precision "none" even when they kept a
        deadline. A kept phrase becomes unresolved, and an instant without its phrase is
        dropped, so these rows still validate. Pass ``suggestions`` in position order; a
        suggestion row that no longer validates raises ValueError.
        """
        precision = DeadlinePrecision(row.deadline_precision)
        deadline_text = row.deadline_text or None
        deadline_date, deadline_at_utc, deadline_timezone = (
            row.deadline_date,
            row.deadline_at_utc,
            row.deadline_timezone,
        )
        if precision is DeadlinePrecision.NONE:
            if deadline_text is not None:
                precision = DeadlinePrecision.UNRESOLVED
            deadline_date = deadline_at_utc = deadline_timezone = None
        return MessageAnalysis(
            message_key=message_key,
            category=AnalysisCategory(row.category),
            summary=row.summary,
            action_required=row.action_required,
            action_text=row.action_text,
            deadline_text=deadline_text,
            deadline_precision=precision,
            deadline_date=deadline_date,
            deadline_at_utc=deadline_at_utc,
            deadline_timezone=deadline_timezone,
            confidence=row.confidence,
            evidence=row.evidence,
            suggestions=tuple(suggestion_from_row(item) for item in suggestions),
            follow_up=FollowUpKind(row.follow_up_kind),
            follow_up_evidence=row.follow_up_evidence,
        )


_COVERAGE_COLUMNS = (
    "sync_complete",
    "shortlisted_count",
    "analyzed_count",
    "reused_count",
    "failed_count",
    "skipped_count",
    "input_tokens",
    "output_tokens",
    "ai_provider",
    "ai_model",
)


def _coverage_columns(coverage: DigestCoverage | None) -> dict[str, bool | int | str | None]:
    """Map brief coverage onto digest columns; no coverage leaves every column NULL, except
    the deferred count, which is never NULL."""
    if coverage is None:
        return {**dict.fromkeys(_COVERAGE_COLUMNS), "deferred_count": 0}
    return {
        "sync_complete": coverage.sync_complete,
        "shortlisted_count": coverage.shortlisted,
        "analyzed_count": coverage.analyzed,
        "reused_count": coverage.reused,
        "failed_count": coverage.failed,
        "skipped_count": coverage.skipped,
        "deferred_count": coverage.deferred,
        "input_tokens": coverage.input_tokens,
        "output_tokens": coverage.output_tokens,
        "ai_provider": coverage.ai_provider,
        "ai_model": coverage.ai_model,
    }


def _fallback_summary(preview: str, subject: str) -> str:
    """Summarize an unanalyzed item from its preview, else its subject, within the item limit."""
    for text in (preview.strip(), subject.strip()):
        if text:
            # Previews (up to 255 characters in Outlook) can exceed the summary limit.
            return truncate_at_boundary(text, SUMMARY_MAX_CHARS)[0]
    return _NO_SUMMARY


def _displayed_precision(analysis: AnalysisTable) -> DeadlinePrecision:
    """The precision to show; rows saved before migration 0004 read "none" even with a deadline.

    A legacy instant shows as exact, keeping its phrase; a legacy phrase alone shows as
    unresolved.
    """
    precision = DeadlinePrecision(analysis.deadline_precision)
    if precision is DeadlinePrecision.NONE:
        if analysis.deadline_at_utc is not None:
            return DeadlinePrecision.DATETIME
        if analysis.deadline_text:
            return DeadlinePrecision.UNRESOLVED
    return precision


def _coverage_from_row(digest: DigestTable) -> DigestCoverage | None:
    """Rebuild brief coverage; briefs saved before M4 have none."""
    if digest.shortlisted_count is None:
        return None
    return DigestCoverage(
        sync_complete=bool(digest.sync_complete),
        shortlisted=digest.shortlisted_count,
        analyzed=digest.analyzed_count or 0,
        reused=digest.reused_count or 0,
        failed=digest.failed_count or 0,
        skipped=digest.skipped_count or 0,
        deferred=digest.deferred_count,
        input_tokens=digest.input_tokens,
        output_tokens=digest.output_tokens,
        ai_provider=digest.ai_provider,
        ai_model=digest.ai_model,
    )


class DigestRepository:
    """Repository managing generated daily digests and digest item membership."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_digest(
        self,
        account_id: int,
        local_date: date,
        timezone_name: str,
        status: str | DigestStatus,
        items: Sequence[tuple[int, int | None, int, str | DigestSection]] = (),
        coverage: DigestCoverage | None = None,
    ) -> DigestTable:
        """Create or update a daily digest, its coverage and its ordered items atomically."""
        status_val = status.value if isinstance(status, DigestStatus) else str(status)
        coverage_values = _coverage_columns(coverage)
        base_stmt = sqlite_insert(DigestTable).values(
            account_id=account_id,
            local_date=local_date,
            timezone_name=timezone_name,
            status=status_val,
            generated_at_utc=datetime.now(UTC),
            **coverage_values,
        )
        stmt = (
            base_stmt.on_conflict_do_update(
                index_elements=["account_id", "local_date"],
                set_={
                    "timezone_name": base_stmt.excluded.timezone_name,
                    "status": base_stmt.excluded.status,
                    "generated_at_utc": base_stmt.excluded.generated_at_utc,
                    **{name: base_stmt.excluded[name] for name in coverage_values},
                },
            )
            .returning(DigestTable)
            # Refresh a digest already loaded in this session instead of returning it stale.
            .execution_options(populate_existing=True)
        )
        digest = await self._session.scalar(stmt)
        assert digest is not None

        # Delete any previous items for this digest and insert the new item list
        await self._session.execute(
            delete(DigestItemTable).where(DigestItemTable.digest_id == digest.id)
        )

        if items:
            item_rows = [
                {
                    "digest_id": digest.id,
                    "message_id": msg_id,
                    "analysis_id": analysis_id,
                    "position": pos,
                    "section": (sec.value if isinstance(sec, DigestSection) else str(sec)),
                }
                for msg_id, analysis_id, pos, sec in items
            ]
            await self._session.execute(insert(DigestItemTable).values(item_rows))

        return digest

    async def get_by_account_and_date(
        self,
        account_id: int,
        local_date: date,
    ) -> DigestTable | None:
        """Fetch a digest by its unique account and local date combination."""
        stmt = select(DigestTable).where(
            DigestTable.account_id == account_id,
            DigestTable.local_date == local_date,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def message_ids_for_date(
        self, account_id: int, local_date: date
    ) -> tuple[tuple[str, ...], frozenset[str]]:
        """The provider message IDs in the saved brief of one account and day, in brief order,
        and which of them the brief lists as replies from outside today's Inbox."""
        result = await self._session.execute(
            select(MessageTable.provider_message_id, DigestItemTable.section)
            .join(DigestItemTable, DigestItemTable.message_id == MessageTable.id)
            .join(DigestTable, DigestTable.id == DigestItemTable.digest_id)
            .where(DigestTable.account_id == account_id, DigestTable.local_date == local_date)
            .order_by(DigestItemTable.position)
        )
        rows = result.all()
        return (
            tuple(key for key, _ in rows),
            frozenset(key for key, section in rows if section == DigestSection.FOLLOW_UPS.value),
        )

    async def get_latest(self) -> DailyDigest | None:
        """Restore the brief for the newest local day across all local Gmail accounts,
        regardless of sign-in.

        The newest day wins, not the newest save: a brief made today for a missed day never
        replaces today's. The desktop shows its account explicitly; offline startup needs
        no active session.
        """
        result = await self._session.execute(
            select(DigestTable, AccountTable)
            .join(AccountTable, DigestTable.account_id == AccountTable.id)
            .where(AccountTable.provider == ProviderKind.GMAIL.value)
            .order_by(
                DigestTable.local_date.desc(),
                DigestTable.generated_at_utc.desc(),
                DigestTable.id.desc(),
            )
            .limit(1)
        )
        row = result.first()
        if row is None:
            return None
        digest, account = row
        return await self._domain(digest, account.email_address)

    async def get_for_account_date(
        self, account_email: str, local_date: date
    ) -> DailyDigest | None:
        """The saved brief of a Gmail account for one local date, with its suggestions."""
        result = await self._session.execute(
            select(DigestTable)
            .join(AccountTable, DigestTable.account_id == AccountTable.id)
            .where(
                AccountTable.provider == ProviderKind.GMAIL.value,
                AccountTable.email_address == account_email,
                DigestTable.local_date == local_date,
            )
            .order_by(DigestTable.generated_at_utc.desc(), DigestTable.id.desc())
            .limit(1)
        )
        digest = result.scalar_one_or_none()
        return None if digest is None else await self._domain(digest, account_email)

    async def _domain(self, digest: DigestTable, account_email: str) -> DailyDigest:
        items = await self.get_digest_items(digest.id)
        views = await suggestion_views(
            self._session, [(item.message_id, item.analysis_id) for item, _, _ in items]
        )
        return self.to_domain(digest, items, account_email, suggestions=views)

    async def list_summaries(self, limit: int) -> tuple[SavedBriefSummary, ...]:
        """Saved Gmail briefs, newest local day first, with item counts from one query."""
        item_count = func.count(DigestItemTable.message_id)
        follow_ups = func.coalesce(
            func.sum(case((DigestItemTable.section == DigestSection.FOLLOW_UPS.value, 1), else_=0)),
            0,
        )
        result = await self._session.execute(
            select(
                AccountTable.email_address,
                DigestTable.local_date,
                DigestTable.timezone_name,
                DigestTable.status,
                DigestTable.generated_at_utc,
                item_count,
                follow_ups,
            )
            .join(AccountTable, DigestTable.account_id == AccountTable.id)
            .outerjoin(DigestItemTable, DigestItemTable.digest_id == DigestTable.id)
            .where(AccountTable.provider == ProviderKind.GMAIL.value)
            .group_by(DigestTable.id, AccountTable.email_address)
            .order_by(
                DigestTable.local_date.desc(),
                DigestTable.generated_at_utc.desc(),
                DigestTable.id.desc(),
            )
            .limit(limit)
        )
        return tuple(
            SavedBriefSummary(
                account_email=email,
                local_date=local_date,
                timezone_name=timezone_name,
                status=DigestStatus(status),
                generated_at_utc=generated_at_utc,
                item_count=count,
                follow_up_count=outside,
            )
            for email, local_date, timezone_name, status, generated_at_utc, count, outside in result
        )

    async def saved_dates(self, account_email: str, first: date, last: date) -> frozenset[date]:
        """The local dates from ``first`` to ``last`` with a saved brief for a Gmail account."""
        result = await self._session.scalars(
            select(DigestTable.local_date)
            .join(AccountTable, DigestTable.account_id == AccountTable.id)
            .where(
                AccountTable.provider == ProviderKind.GMAIL.value,
                AccountTable.email_address == account_email,
                DigestTable.local_date >= first,
                DigestTable.local_date <= last,
            )
        )
        return frozenset(result)

    async def accounts_with_brief(self, local_date: date) -> tuple[str, ...]:
        """The Gmail accounts that have a saved brief for a local date, sorted."""
        result = await self._session.scalars(
            select(AccountTable.email_address)
            .join(DigestTable, DigestTable.account_id == AccountTable.id)
            .where(
                AccountTable.provider == ProviderKind.GMAIL.value,
                DigestTable.local_date == local_date,
            )
            .distinct()
            .order_by(AccountTable.email_address)
        )
        return tuple(result)

    async def get_digest_items(
        self,
        digest_id: int,
    ) -> list[tuple[DigestItemTable, MessageTable, AnalysisTable | None]]:
        """Fetch all ordered items for a digest joined with message and analysis records."""
        stmt = (
            select(DigestItemTable, MessageTable, AnalysisTable)
            .join(MessageTable, DigestItemTable.message_id == MessageTable.id)
            .outerjoin(AnalysisTable, DigestItemTable.analysis_id == AnalysisTable.id)
            .where(DigestItemTable.digest_id == digest_id)
            .order_by(DigestItemTable.position.asc())
        )
        result = await self._session.execute(stmt)
        rows = result.tuples().all()
        return cast(
            list[tuple[DigestItemTable, MessageTable, AnalysisTable | None]],
            rows,
        )

    async def get_by_id(self, digest_id: int) -> DigestTable | None:
        """Fetch a digest by its primary key ID."""
        return await self._session.get(DigestTable, digest_id)

    @staticmethod
    def to_domain(
        digest: DigestTable,
        items_with_relations: Sequence[tuple[DigestItemTable, MessageTable, AnalysisTable | None]],
        account_identity: str,
        suggestions: Mapping[int, tuple[SuggestionView, ...]] | None = None,
    ) -> DailyDigest:
        """Map a DigestTable and its joined item records to a DailyDigest domain model.

        ``suggestions`` maps a message row ID to its suggestion views (see
        suggestion_views); an item without an entry shows none.
        """
        views = suggestions or {}
        domain_items: list[DigestItem] = []
        for item_table, msg_table, analysis_table in items_with_relations:
            if analysis_table is None:
                summary_val = _fallback_summary(msg_table.body_preview, msg_table.subject)
            else:
                summary_val = analysis_table.summary or _NO_SUMMARY
            domain_items.append(
                DigestItem(
                    message_key=msg_table.provider_message_id,
                    section=DigestSection(item_table.section),
                    position=item_table.position,
                    subject=msg_table.subject,
                    sender=EmailContact(
                        name=msg_table.sender_name,
                        address=msg_table.sender_address,
                    ),
                    summary=summary_val,
                    action_text=analysis_table.action_text if analysis_table else None,
                    deadline_text=analysis_table.deadline_text if analysis_table else None,
                    deadline_precision=(
                        _displayed_precision(analysis_table)
                        if analysis_table
                        else DeadlinePrecision.NONE
                    ),
                    deadline_date=analysis_table.deadline_date if analysis_table else None,
                    deadline_at_utc=analysis_table.deadline_at_utc if analysis_table else None,
                    evidence=analysis_table.evidence if analysis_table else None,
                    source_url=HttpUrl(msg_table.web_link),
                    suggestions=views.get(msg_table.id, ()),
                )
            )

        return DailyDigest(
            account_id=account_identity,
            local_date=digest.local_date,
            timezone_name=digest.timezone_name,
            generated_at_utc=digest.generated_at_utc,
            status=DigestStatus(digest.status),
            items=tuple(domain_items),
            coverage=_coverage_from_row(digest),
        )


class ConsentRepository:
    """Repository recording per-account consent to send minimized content to an AI provider."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(
        self,
        account_id: int,
        provider: str,
        disclosure_version: str,
    ) -> AIConsentTable | None:
        """Return the consent for this disclosure version unless it has been revoked."""
        stmt = select(AIConsentTable).where(
            AIConsentTable.account_id == account_id,
            AIConsentTable.provider == provider,
            AIConsentTable.disclosure_version == disclosure_version,
            AIConsentTable.revoked_at_utc.is_(None),
        )
        result = await self._session.scalars(stmt.execution_options(populate_existing=True))
        return result.first()

    async def grant(
        self,
        account_id: int,
        provider: str,
        disclosure_version: str,
        now_utc: datetime,
    ) -> AIConsentTable:
        """Record consent, reactivating a revoked grant of the same disclosure version."""
        base_stmt = sqlite_insert(AIConsentTable).values(
            account_id=account_id,
            provider=provider,
            disclosure_version=disclosure_version,
            granted_at_utc=normalize_utc(now_utc),
            revoked_at_utc=None,
        )
        stmt = (
            base_stmt.on_conflict_do_update(
                index_elements=["account_id", "provider", "disclosure_version"],
                set_={
                    "granted_at_utc": base_stmt.excluded.granted_at_utc,
                    "revoked_at_utc": None,
                },
            )
            .returning(AIConsentTable)
            .execution_options(populate_existing=True)
        )
        consent = await self._session.scalar(stmt)
        assert consent is not None
        return consent

    async def with_auto_send(self, provider: str) -> list[AIConsentTable]:
        """Every consent to this provider that holds an automatic-analysis permission
        (ADR 0017), whichever account or disclosure version it belongs to."""
        result = await self._session.scalars(
            select(AIConsentTable)
            .where(AIConsentTable.provider == provider, AIConsentTable.auto_send_limit > 0)
            .order_by(AIConsentTable.id)
            .execution_options(populate_existing=True)
        )
        return list(result)

    async def set_auto_send(
        self,
        account_id: int,
        provider: str,
        disclosure_version: str,
        limit: int,
        now_utc: datetime,
    ) -> AIConsentTable | None:
        """Set how many messages an automatic run may send without asking (ADR 0017), on the
        active consent for this disclosure version; None when there is no active consent.

        A limit above 0 records when it was given; 0 clears both. Raises ValueError for a
        limit outside 0 to AUTO_SEND_LIMIT_MAX. The consent changes as an ORM object, so a
        loaded copy always shows it.
        """
        if not 0 <= limit <= AUTO_SEND_LIMIT_MAX:
            raise ValueError(f"The limit must be 0 to {AUTO_SEND_LIMIT_MAX}.")
        consent = await self.get_active(account_id, provider, disclosure_version)
        if consent is None:
            return None
        consent.auto_send_limit = limit
        consent.auto_send_granted_at_utc = normalize_utc(now_utc) if limit else None
        await self._session.flush()
        return consent

    async def revoke_all(self, account_id: int, provider: str, now_utc: datetime) -> int:
        """Revoke every active consent for the provider and return how many were revoked.

        A revoked consent keeps no automatic-analysis permission (ADR 0017).
        """
        stmt = (
            update(AIConsentTable)
            .where(
                AIConsentTable.account_id == account_id,
                AIConsentTable.provider == provider,
                AIConsentTable.revoked_at_utc.is_(None),
            )
            .values(
                revoked_at_utc=normalize_utc(now_utc),
                auto_send_limit=0,
                auto_send_granted_at_utc=None,
            )
            .returning(AIConsentTable.id)
        )
        revoked = await self._session.scalars(stmt)
        return len(revoked.all())


class OwnerConsentRepository:
    """The owner's consents that belong to no account, such as AI drafting (ADR 0013).

    Rows change through ORM objects, so loaded consents always show the latest state.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _find(
        self, provider: str, scope: str, disclosure_version: str
    ) -> OwnerConsentTable | None:
        result = await self._session.scalars(
            select(OwnerConsentTable).where(
                OwnerConsentTable.provider == provider,
                OwnerConsentTable.scope == scope,
                OwnerConsentTable.disclosure_version == disclosure_version,
            )
        )
        return result.first()

    async def get_active(
        self, provider: str, scope: str, disclosure_version: str
    ) -> OwnerConsentTable | None:
        """The consent to this disclosure version, unless it has been revoked."""
        consent = await self._find(provider, scope, disclosure_version)
        return consent if consent is not None and consent.revoked_at_utc is None else None

    async def grant(
        self, provider: str, scope: str, disclosure_version: str, now_utc: datetime
    ) -> OwnerConsentTable:
        """Record consent, reactivating a revoked grant of the same disclosure version."""
        consent = await self._find(provider, scope, disclosure_version)
        if consent is None:
            consent = OwnerConsentTable(
                provider=provider, scope=scope, disclosure_version=disclosure_version
            )
            self._session.add(consent)
        consent.granted_at_utc = normalize_utc(now_utc)
        consent.revoked_at_utc = None
        await self._session.flush()
        return consent

    async def revoke_all(self, provider: str, now_utc: datetime) -> int:
        """Revoke every active consent to the provider, in every scope; return how many."""
        result = await self._session.scalars(
            select(OwnerConsentTable).where(
                OwnerConsentTable.provider == provider,
                OwnerConsentTable.revoked_at_utc.is_(None),
            )
        )
        active = list(result.all())
        for consent in active:
            consent.revoked_at_utc = normalize_utc(now_utc)
        return len(active)


class SyncRunRepository:
    """Repository managing audit history for synchronization runs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_sync_run(
        self,
        account_id: int,
        range_start_utc: datetime,
        range_end_utc: datetime,
        status: str | SyncStatus = "started",
    ) -> SyncRunTable:
        """Record the initiation of a mailbox synchronization run."""
        status_val = status.value if isinstance(status, SyncStatus) else str(status)
        sync_run = SyncRunTable(
            account_id=account_id,
            range_start_utc=normalize_utc(range_start_utc),
            range_end_utc=normalize_utc(range_end_utc),
            started_at_utc=datetime.now(UTC),
            status=status_val,
            page_count=0,
            message_count=0,
        )
        self._session.add(sync_run)
        await self._session.flush()
        return sync_run

    async def update_sync_run(
        self,
        sync_run_id: int,
        *,
        status: str | SyncStatus | None = None,
        page_count: int | None = None,
        message_count: int | None = None,
        failed_message_count: int | None = None,
        error_code: str | None = None,
        completed: bool = False,
    ) -> SyncRunTable | None:
        """Update progress or terminal state of an existing sync run."""
        sync_run = await self._session.get(SyncRunTable, sync_run_id)
        if not sync_run:
            return None

        if status is not None:
            sync_run.status = status.value if isinstance(status, SyncStatus) else str(status)
        if page_count is not None:
            sync_run.page_count = page_count
        if message_count is not None:
            sync_run.message_count = message_count
        if failed_message_count is not None:
            sync_run.failed_message_count = failed_message_count
        if error_code is not None:
            sync_run.sanitized_error_code = error_code
        if completed:
            sync_run.completed_at_utc = datetime.now(UTC)

        await self._session.flush()
        return sync_run

    async def get_latest_sync_run(self, account_id: int) -> SyncRunTable | None:
        """Retrieve the most recent sync run record for an account."""
        stmt = (
            select(SyncRunTable)
            .where(SyncRunTable.account_id == account_id)
            .order_by(SyncRunTable.started_at_utc.desc())
            .limit(1)
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_id(self, sync_run_id: int) -> SyncRunTable | None:
        """Fetch a sync run record by its primary key ID."""
        return await self._session.get(SyncRunTable, sync_run_id)
