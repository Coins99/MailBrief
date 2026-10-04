"""Offline owner exports and explicit, previewed retention. No provider or vault access."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.infra.files import write_text_atomically
from mailbrief.storage.database import Database
from mailbrief.storage.tables import (
    AccountTable,
    ActionTable,
    Base,
    DigestTable,
    DraftTable,
    MessageTable,
    SuggestionDecisionTable,
    SyncRunTable,
)

EXPORT_TABLES = (
    "actions",
    "action_steps",
    "action_sources",
    "action_proposals",
    "drafts",
    "draft_versions",
    "draft_sources",
    "draft_generations",
)


class CleanupKind(StrEnum):
    CACHE = "cache"
    ACCOUNT = "account"
    DELETED = "deleted"
    ORPHAN_DECISIONS = "orphan-decisions"


@dataclass(frozen=True)
class CleanupRequest:
    kind: CleanupKind
    account_id: int | None = None
    before: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CleanupKind):
            raise ValueError("Unknown cleanup operation.")
        if self.before is not None and (
            self.before.tzinfo is None or self.before.utcoffset() is None
        ):
            raise ValueError("Cleanup needs an aware cutoff.")
        if self.kind in (CleanupKind.CACHE, CleanupKind.ACCOUNT):
            if self.account_id is None or self.account_id < 1:
                raise ValueError("Choose a saved account.")
        elif self.account_id is not None or self.before is None:
            raise ValueError("Deleted-writing cleanup needs a cutoff and no account.")
        if self.kind == CleanupKind.ACCOUNT and self.before is not None:
            raise ValueError("Account removal has no date cutoff.")


@dataclass(frozen=True)
class CleanupPreview:
    request: CleanupRequest
    fingerprint: str
    messages: int
    briefs: int
    sync_runs: int
    accounts: int
    actions: int
    drafts: int
    versions: int
    decisions: int

    @property
    def summary(self) -> str:
        return (
            f"Remove {self.messages} cached messages, {self.briefs} saved briefs, "
            f"{self.sync_runs} sync records, {self.accounts} accounts, "
            f"{self.actions} deleted actions, {self.drafts} deleted drafts/notes "
            f"{self.versions} draft versions and {self.decisions} orphaned decisions."
        )


class CleanupChangedError(ValueError):
    def __init__(self) -> None:
        super().__init__("Saved data changed. Review a new cleanup preview.")


def _json(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("Unsupported export value.")


async def _records(session: AsyncSession, names: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        table = Base.metadata.tables[name]
        rows = await session.execute(select(table).order_by(*table.primary_key.columns))
        result[name] = [dict(row) for row in rows.mappings()]
    return result


async def export_writing(database: Database, destination: Path) -> None:
    """Export all owner writing, including deleted rows, versions and source snapshots.

    Original table IDs retain relationships; this is a portable UTF-8 JSON format,
    not a database restore. It contains no mailbox cache, downloaded bodies or secrets.
    """
    async with database.session() as session:
        await session.execute(text("BEGIN"))
        records = await _records(session, EXPORT_TABLES)
    payload = (
        await asyncio.to_thread(
            json.dumps,
            {
                "format": "mailbrief-writing",
                "version": 1,
                "created_at_utc": datetime.now(UTC).isoformat(),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
            default=_json,
        )
        + "\n"
    )
    await asyncio.to_thread(write_text_atomically, destination, payload, overwrite=False)


async def _targets(session: AsyncSession, request: CleanupRequest) -> list[Any]:
    if request.kind == CleanupKind.ORPHAN_DECISIONS:
        identities = set(
            (
                await session.execute(
                    select(AccountTable.provider, AccountTable.provider_account_id)
                )
            ).tuples()
        )
        return [
            row
            for row in await session.scalars(
                select(SuggestionDecisionTable).where(
                    SuggestionDecisionTable.action_id.is_(None),
                    SuggestionDecisionTable.decided_at_utc < request.before,
                )
            )
            if (row.provider, row.provider_account_id) not in identities and row.provider == "gmail"
        ]
    if request.kind == CleanupKind.DELETED:
        targets: list[Any] = []
        for owner_class in (ActionTable, DraftTable):
            targets.extend(
                await session.scalars(
                    select(owner_class).where(owner_class.deleted_at_utc < request.before)
                )
            )
        return targets
    account = await session.get(AccountTable, request.account_id)
    if account is None or account.provider != "gmail":
        raise ValueError("Choose a saved Gmail account.")
    if request.kind == CleanupKind.ACCOUNT:
        return [account]
    targets = []
    for cls, timestamp in (
        (MessageTable, MessageTable.received_at_utc),
        (DigestTable, DigestTable.generated_at_utc),
        (SyncRunTable, SyncRunTable.started_at_utc),
    ):
        statement = select(cls).where(cls.account_id == account.id)
        if request.before is not None:
            statement = statement.where(timestamp < request.before)
        targets.extend(await session.scalars(statement))
    return targets


async def _preview(session: AsyncSession, request: CleanupRequest) -> CleanupPreview:
    # Binding to every stored row also detects new dependencies and changes to writing
    # between review and apply. The fingerprint contains no readable owner content.
    records = await _records(session, tuple(sorted(Base.metadata.tables)))
    fingerprint = await asyncio.to_thread(
        lambda: hashlib.sha256(
            json.dumps(records, sort_keys=True, default=_json, separators=(",", ":")).encode()
        ).hexdigest()
    )
    targets = await _targets(session, request)
    ids = {type(row): {r.id for r in targets if type(r) is type(row)} for row in targets}
    accounts = ids.get(AccountTable, set())
    messages = ids.get(MessageTable, set()) | {
        row["id"] for row in records["messages"] if row["account_id"] in accounts
    }
    # Message cascades remove digest membership, not the brief header. Count headers
    # selected by age/account separately; the UI explains that older members disappear.
    drafts = ids.get(DraftTable, set())
    return CleanupPreview(
        request,
        fingerprint,
        len(messages),
        len(ids.get(DigestTable, set()))
        + sum(row["account_id"] in accounts for row in records["digests"]),
        len(ids.get(SyncRunTable, set()))
        + sum(row["account_id"] in accounts for row in records["sync_runs"]),
        len(accounts),
        len(ids.get(ActionTable, set())),
        len(drafts),
        sum(row["draft_id"] in drafts for row in records["draft_versions"]),
        len(ids.get(SuggestionDecisionTable, set())),
    )


async def preview_cleanup(database: Database, request: CleanupRequest) -> CleanupPreview:
    async with database.session() as session:
        await session.execute(text("BEGIN"))
        return await _preview(session, request)


async def apply_cleanup(database: Database, preview: CleanupPreview) -> None:
    async with database.session() as session:
        # Lock before rechecking. Nothing new can enter the reviewed deletion set.
        await session.execute(text("BEGIN IMMEDIATE"))
        current = await _preview(session, preview.request)
        if current != preview:
            raise CleanupChangedError()
        for row in await _targets(session, preview.request):
            await session.delete(row)
        if preview.request.kind == CleanupKind.CACHE:
            account = await session.get(AccountTable, preview.request.account_id)
            if account is not None:
                account.last_sync_at_utc = None
        await session.commit()
