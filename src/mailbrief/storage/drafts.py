"""The owner's drafts, their saved versions and their source snapshots (ADR 0012).

Drafts and versions change through ORM objects only, never bulk statements, so rows
already loaded in the session always show what was last written.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import HttpUrl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.drafting import DraftContextPart, DraftGenerationInfo
from mailbrief.domain.drafts import (
    Draft,
    DraftEdit,
    DraftGenerationSummary,
    DraftKind,
    DraftLength,
    DraftSource,
    DraftSummary,
    DraftTone,
    DraftVersion,
    DraftVersionInfo,
    DraftVersionOrigin,
    first_line,
)
from mailbrief.storage.database import chunked
from mailbrief.storage.tables import (
    ActionSourceTable,
    ActionTable,
    DraftGenerationTable,
    DraftSourceTable,
    DraftTable,
    DraftVersionTable,
    MessageTable,
)


def content_of(row: DraftTable | DraftVersionTable) -> DraftEdit:
    """The owner's text held by a draft or a version."""
    return DraftEdit(title=row.title, to_text=row.to_text, cc_text=row.cc_text, body=row.body)


def apply_content(row: DraftTable, edit: DraftEdit) -> None:
    row.title = edit.title
    row.to_text = edit.to_text
    row.cc_text = edit.cc_text
    row.body = edit.body


def _source(row: DraftSourceTable) -> DraftSource:
    return DraftSource(
        provider_message_id=row.provider_message_id,
        subject=row.subject,
        sender_address=row.sender_address,
        web_link=HttpUrl(row.web_link),
        received_at_utc=row.received_at_utc,
        available=row.message_id is not None,
    )


def draft_from_rows(
    row: DraftTable, action_public_id: str | None, sources: Sequence[DraftSourceTable]
) -> Draft:
    """Rebuild a draft; ``action_public_id`` is None once its action is gone."""
    return Draft(
        public_id=row.public_id,
        kind=DraftKind(row.kind),
        title=row.title,
        to_text=row.to_text,
        cc_text=row.cc_text,
        body=row.body,
        action_public_id=action_public_id,
        action_title=row.action_title,
        sources=tuple(_source(source) for source in sources),
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
        revision=row.revision,
    )


def generation_info(row: DraftGenerationTable) -> DraftGenerationInfo:
    """Rebuild a generation record; raises ValueError when the row no longer validates."""
    if not isinstance(row.parts_json, list) or not isinstance(row.missing_context_json, list):
        raise ValueError("stored generation lists must be lists")
    return DraftGenerationInfo(
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        tone=DraftTone(row.tone),
        length=DraftLength(row.length),
        parts=frozenset(DraftContextPart(part) for part in row.parts_json),
        instructions=row.instructions,
        missing_context=tuple(row.missing_context_json),
        created_at_utc=row.created_at_utc,
    )


def _summary(row: DraftGenerationTable | None) -> DraftGenerationSummary | None:
    if row is None:
        return None
    try:
        return DraftGenerationSummary(
            tone=DraftTone(row.tone), length=DraftLength(row.length), model=row.model
        )
    except ValueError:
        return None  # A record that no longer validates only loses its label.


def version_info(
    row: DraftVersionTable, generation: DraftGenerationTable | None = None
) -> DraftVersionInfo:
    """A version's list entry: its body's first line, or its title when the body is blank.

    A generated version also says how it was made.
    """
    return DraftVersionInfo(
        number=row.number,
        origin=DraftVersionOrigin(row.origin),
        created_at_utc=row.created_at_utc,
        preview=first_line(row.body) or first_line(row.title),
        length=len(row.body),
        generation=_summary(generation),
    )


def version_from_row(row: DraftVersionTable) -> DraftVersion:
    return DraftVersion(
        number=row.number,
        origin=DraftVersionOrigin(row.origin),
        title=row.title,
        to_text=row.to_text,
        cc_text=row.cc_text,
        body=row.body,
        created_at_utc=row.created_at_utc,
    )


class DraftRepository:
    """Drafts with their versions and sources; the service owns commits and rollbacks."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_draft(self, row: DraftTable) -> DraftTable:
        """Add a new draft and flush it, so it has an ID."""
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_draft(
        self, public_id: str, *, include_deleted: bool = False
    ) -> DraftTable | None:
        stmt = select(DraftTable).where(DraftTable.public_id == public_id)
        if not include_deleted:
            stmt = stmt.where(DraftTable.deleted_at_utc.is_(None))
        return (await self._session.scalars(stmt)).first()

    async def latest_version(self, draft_id: int) -> DraftVersionTable | None:
        result = await self._session.scalars(
            select(DraftVersionTable)
            .where(DraftVersionTable.draft_id == draft_id)
            .order_by(DraftVersionTable.number.desc())
            .limit(1)
        )
        return result.first()

    async def get_version(self, draft_id: int, number: int) -> DraftVersionTable | None:
        result = await self._session.scalars(
            select(DraftVersionTable).where(
                DraftVersionTable.draft_id == draft_id, DraftVersionTable.number == number
            )
        )
        return result.first()

    async def version_rows(
        self, draft_id: int
    ) -> list[tuple[DraftVersionTable, DraftGenerationTable | None]]:
        """Every kept version, newest first, with its generation record when it has one."""
        result = await self._session.execute(
            select(DraftVersionTable, DraftGenerationTable)
            .outerjoin(
                DraftGenerationTable, DraftGenerationTable.version_id == DraftVersionTable.id
            )
            .where(DraftVersionTable.draft_id == draft_id)
            .order_by(DraftVersionTable.number.desc())
        )
        return [(version, generation) for version, generation in result.tuples()]

    async def get_generation(self, version_id: int) -> DraftGenerationTable | None:
        result = await self._session.scalars(
            select(DraftGenerationTable).where(DraftGenerationTable.version_id == version_id)
        )
        return result.first()

    async def add_generation(self, version_id: int, info: DraftGenerationInfo) -> None:
        """Record how a generated version was made."""
        self._session.add(
            DraftGenerationTable(
                version_id=version_id,
                provider=info.provider,
                model=info.model,
                prompt_version=info.prompt_version,
                tone=info.tone.value,
                length=info.length.value,
                parts_json=sorted(part.value for part in info.parts),
                instructions=info.instructions,
                missing_context_json=list(info.missing_context),
                created_at_utc=info.created_at_utc,
            )
        )
        await self._session.flush()

    async def add_version(
        self, draft_id: int, origin: DraftVersionOrigin, content: DraftEdit, now: datetime
    ) -> DraftVersionTable:
        """Save ``content`` as the draft's next version number and flush it."""
        highest = await self._session.scalar(
            select(func.max(DraftVersionTable.number)).where(DraftVersionTable.draft_id == draft_id)
        )
        row = DraftVersionTable(
            draft_id=draft_id,
            number=(highest or 0) + 1,
            origin=origin.value,
            title=content.title,
            to_text=content.to_text,
            cc_text=content.cc_text,
            body=content.body,
            created_at_utc=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def prune_versions(self, draft_id: int, keep: int) -> int:
        """Delete all but the newest ``keep`` versions, one ORM object at a time."""
        result = await self._session.scalars(
            select(DraftVersionTable)
            .where(DraftVersionTable.draft_id == draft_id)
            .order_by(DraftVersionTable.number.desc())
            .offset(keep)
        )
        old = list(result.all())
        for row in old:
            generation = await self.get_generation(row.id)
            if generation is not None:
                await self._session.delete(generation)
            await self._session.delete(row)
        return len(old)

    async def source_rows(self, draft_id: int) -> list[DraftSourceTable]:
        result = await self._session.scalars(
            select(DraftSourceTable)
            .where(DraftSourceTable.draft_id == draft_id)
            .order_by(DraftSourceTable.id)
        )
        return list(result.all())

    async def action_source_rows(self, action_id: int) -> list[ActionSourceTable]:
        """An action's source snapshots, oldest first, to copy into a draft."""
        result = await self._session.scalars(
            select(ActionSourceTable)
            .where(ActionSourceTable.action_id == action_id)
            .order_by(ActionSourceTable.id)
        )
        return list(result.all())

    def add_message_source(self, draft_id: int, message: MessageTable) -> None:
        """Link a cached message by snapshot."""
        self._session.add(
            DraftSourceTable(
                draft_id=draft_id,
                message_id=message.id,
                provider_message_id=message.provider_message_id,
                subject=message.subject,
                sender_address=message.sender_address,
                web_link=message.web_link,
                received_at_utc=message.received_at_utc,
            )
        )

    def copy_source(self, draft_id: int, source: ActionSourceTable | DraftSourceTable) -> None:
        """Copy another snapshot, keeping its message link when the message is still cached."""
        self._session.add(
            DraftSourceTable(
                draft_id=draft_id,
                message_id=source.message_id,
                provider_message_id=source.provider_message_id,
                subject=source.subject,
                sender_address=source.sender_address,
                web_link=source.web_link,
                received_at_utc=source.received_at_utc,
            )
        )

    async def action_public_ids(self, action_ids: Iterable[int]) -> dict[int, str]:
        """Live actions' public IDs by row ID; a soft-deleted action is not linked."""
        found: dict[int, str] = {}
        for chunk in chunked(action_ids):
            result = await self._session.execute(
                select(ActionTable.id, ActionTable.public_id).where(
                    ActionTable.id.in_(chunk), ActionTable.deleted_at_utc.is_(None)
                )
            )
            found.update(result.tuples().all())
        return found

    async def load(self, rows: Sequence[DraftTable]) -> list[Draft]:
        """Domain drafts for rows, reading sources and action links in chunked queries."""
        ids = [row.id for row in rows]
        sources: dict[int, list[DraftSourceTable]] = {}
        for chunk in chunked(ids):
            result = await self._session.scalars(
                select(DraftSourceTable)
                .where(DraftSourceTable.draft_id.in_(chunk))
                .order_by(DraftSourceTable.draft_id, DraftSourceTable.id)
            )
            for source in result:
                sources.setdefault(source.draft_id, []).append(source)
        actions = await self.action_public_ids(
            row.action_id for row in rows if row.action_id is not None
        )
        return [
            draft_from_rows(
                row,
                None if row.action_id is None else actions.get(row.action_id),
                sources.get(row.id, ()),
            )
            for row in rows
        ]

    async def list_summaries(self) -> list[DraftSummary]:
        """Every live draft, most recently updated first; versions are never read."""
        rows = list(
            (
                await self._session.scalars(
                    select(DraftTable)
                    .where(DraftTable.deleted_at_utc.is_(None))
                    .order_by(DraftTable.updated_at_utc.desc(), DraftTable.id.desc())
                )
            ).all()
        )
        subjects: dict[int, str] = {}
        for chunk in chunked(row.id for row in rows):
            result = await self._session.execute(
                select(DraftSourceTable.draft_id, DraftSourceTable.subject)
                .where(DraftSourceTable.draft_id.in_(chunk))
                .order_by(DraftSourceTable.draft_id, DraftSourceTable.id)
            )
            for draft_id, subject in result.tuples():
                subjects.setdefault(draft_id, subject)
        summaries: list[DraftSummary] = []
        for row in rows:
            draft = draft_from_rows(row, None, ())
            summaries.append(
                DraftSummary(
                    public_id=draft.public_id,
                    kind=draft.kind,
                    display_title=draft.display_title,
                    updated_at_utc=draft.updated_at_utc,
                    placeholder_count=len(draft.placeholders),
                    action_title=draft.action_title,
                    source_subject=subjects.get(row.id),
                    revision=draft.revision,
                )
            )
        return summaries
