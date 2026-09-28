"""The owner's drafts and notes: create, autosave, version, restore and delete (ADR 0012).

Nothing here reads or sends email. A reply is built from the cached message row alone
(subject and sender); no body is ever downloaded or quoted. Drafting has no "sent" state
and never changes an action. Errors carry static messages, and nothing logs draft text.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.common import normalize_utc
from mailbrief.domain.drafts import (
    DRAFT_TITLE_MAX_CHARS,
    EMAIL_KINDS,
    MAX_DRAFT_VERSIONS,
    Draft,
    DraftEdit,
    DraftKind,
    DraftSummary,
    DraftVersion,
    DraftVersionInfo,
    DraftVersionOrigin,
)
from mailbrief.services.actions import ActionNotFoundError
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.drafts import (
    DraftRepository,
    apply_content,
    content_of,
    version_from_row,
    version_info,
)
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.storage.tables import ActionSourceTable, DraftTable
from mailbrief.text.prepare import clean_generated_text

UNKNOWN_SENDER: Final = "unknown@invalid"  # What the Gmail adapter stores without a From.
_NOT_FOUND: Final = "That draft was not found."
_VERSION_NOT_FOUND: Final = "That draft version was not found."
_SOURCE_NOT_FOUND: Final = "That email is no longer in local mail."
_ACTION_NOT_FOUND: Final = "That action was not found."
_STALE: Final = "The draft changed since it was loaded; save your text as a new draft."
_NO_RECIPIENTS: Final = "Only replies and emails have recipients."
_REPLY_PREFIX = re.compile(r"re\s*[:：]", re.IGNORECASE)


class DraftNotFoundError(LookupError):
    """No live draft has that public ID, or the draft has no such version."""


class DraftConflictError(ValueError):
    """The draft changed since it was read, or the change does not fit its kind."""


class SourceNotFoundError(LookupError):
    """The email to reply to is not in local mail."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _touch(row: DraftTable, now: datetime) -> None:
    row.revision += 1
    row.updated_at_utc = now


def reply_title(subject: str) -> str:
    """ "Re: <subject>", unless the subject already starts with "Re:" in any case or width.

    The subject comes from an email, so control and format characters are removed and it
    becomes one line.
    """
    cleaned = clean_generated_text(subject)
    title = cleaned if _REPLY_PREFIX.match(cleaned) else f"Re: {cleaned}".rstrip()
    return title[:DRAFT_TITLE_MAX_CHARS].rstrip()


def reply_recipient(sender_address: str) -> str:
    """The sender to reply to; an unknown sender leaves To empty rather than guessed."""
    return "" if sender_address == UNKNOWN_SENDER else sender_address


class DraftService:
    """The owner's drafts. Every mutating method reads the clock once, commits once and
    rolls back on failure.

    A method taking ``expected_revision`` raises DraftConflictError when the draft's
    revision differs, so a stale editor never overwrites newer text. Versions are saved
    only when asked (checkpoint, restore, creation); autosave only updates the draft.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_public_id,
    ) -> None:
        self._session = session
        self._clock = clock
        self._id_factory = id_factory
        self._repository = DraftRepository(session)

    async def _write[T](self, operation: Callable[[datetime], Awaitable[T]]) -> T:
        now = normalize_utc(self._clock())
        try:
            result = await operation(now)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return result

    async def _live(self, public_id: str, expected_revision: int | None) -> DraftTable:
        row = await self._repository.get_draft(public_id)
        if row is None:
            raise DraftNotFoundError(_NOT_FOUND)
        if expected_revision is not None and row.revision != expected_revision:
            raise DraftConflictError(_STALE)
        return row

    async def _load(self, row: DraftTable) -> Draft:
        return (await self._repository.load([row]))[0]

    async def _new_draft(
        self,
        kind: DraftKind,
        content: DraftEdit,
        now: datetime,
        *,
        action_id: int | None = None,
        action_title: str | None = None,
    ) -> DraftTable:
        """Add a draft holding ``content`` and its first version, origin "created"."""
        if kind not in EMAIL_KINDS and content.has_recipients():
            raise DraftConflictError(_NO_RECIPIENTS)
        row = await self._repository.add_draft(
            DraftTable(
                public_id=self._id_factory(),
                kind=kind.value,
                title=content.title,
                to_text=content.to_text,
                cc_text=content.cc_text,
                body=content.body,
                action_id=action_id,
                action_title=action_title,
                created_at_utc=now,
                updated_at_utc=now,
                revision=1,
            )
        )
        await self._repository.add_version(row.id, DraftVersionOrigin.CREATED, content, now)
        return row

    async def _save_version(
        self, row: DraftTable, origin: DraftVersionOrigin, now: datetime
    ) -> DraftVersionInfo | None:
        """Save the draft's text as a version unless it matches the latest; then prune."""
        latest = await self._repository.latest_version(row.id)
        current = content_of(row)
        if latest is not None and content_of(latest) == current:
            return None
        saved = await self._repository.add_version(row.id, origin, current, now)
        await self._repository.prune_versions(row.id, MAX_DRAFT_VERSIONS)
        return version_info(saved)

    async def create(self, kind: DraftKind) -> Draft:
        """A blank draft of ``kind``."""

        async def run(now: datetime) -> Draft:
            return await self._load(await self._new_draft(kind, DraftEdit(), now))

        return await self._write(run)

    async def create_reply(self, account_email: str, provider_message_id: str) -> Draft:
        """A reply to a cached message, addressed to its sender, with "Re:" in the title.

        Only the cached row is read: its subject, sender, link and received time.
        """

        async def run(now: datetime) -> Draft:
            account = await AccountRepository(self._session).get_by_email(account_email)
            message = (
                None
                if account is None
                else await MessageRepository(self._session).get_by_provider_message_id(
                    account.id, provider_message_id
                )
            )
            if message is None:
                raise SourceNotFoundError(_SOURCE_NOT_FOUND)
            content = DraftEdit(
                title=reply_title(message.subject),
                to_text=reply_recipient(message.sender_address),
            )
            row = await self._new_draft(DraftKind.REPLY, content, now)
            self._repository.add_message_source(row.id, message)
            await self._session.flush()
            return await self._load(row)

        return await self._write(run)

    async def create_for_action(self, public_id: str, kind: DraftKind) -> Draft:
        """A draft linked to a live action, with a copy of each of its source snapshots.

        A reply answers the action's first email still in local mail, taking its subject
        and sender like create_reply; without one it raises SourceNotFoundError. Other
        kinds take the action's title.
        """

        async def run(now: datetime) -> Draft:
            actions = ActionRepository(self._session)
            action = await actions.get_action(public_id)
            if action is None:
                raise ActionNotFoundError(_ACTION_NOT_FOUND)
            sources = await self._repository.action_source_rows(action.id)
            if kind is DraftKind.REPLY:
                replied = _first_available(sources)
                if replied is None:
                    raise SourceNotFoundError(_SOURCE_NOT_FOUND)
                content = DraftEdit(
                    title=reply_title(replied.subject),
                    to_text=reply_recipient(replied.sender_address),
                )
            else:
                content = DraftEdit(title=action.title[:DRAFT_TITLE_MAX_CHARS])
            row = await self._new_draft(
                kind, content, now, action_id=action.id, action_title=action.title
            )
            for source in sources:
                self._repository.copy_source(row.id, source)
            await self._session.flush()
            return await self._load(row)

        return await self._write(run)

    async def autosave(self, public_id: str, expected_revision: int, edit: DraftEdit) -> Draft:
        """Save the owner's current text as the draft's next revision, without a version."""

        async def run(now: datetime) -> Draft:
            row = await self._live(public_id, expected_revision)
            if DraftKind(row.kind) not in EMAIL_KINDS and edit.has_recipients():
                raise DraftConflictError(_NO_RECIPIENTS)
            apply_content(row, edit)
            _touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def checkpoint(self, public_id: str, expected_revision: int) -> DraftVersionInfo | None:
        """Save the draft's text as an "edited" version; None when it matches the latest.

        The draft itself is unchanged, so its revision stays the same.
        """

        async def run(now: datetime) -> DraftVersionInfo | None:
            row = await self._live(public_id, expected_revision)
            return await self._save_version(row, DraftVersionOrigin.EDITED, now)

        return await self._write(run)

    async def versions(self, public_id: str) -> tuple[DraftVersionInfo, ...]:
        """A live draft's kept versions, newest first; generated ones say how they were made."""
        row = await self._live(public_id, None)
        return tuple(
            version_info(version, generation)
            for version, generation in await self._repository.version_rows(row.id)
        )

    async def version(self, public_id: str, number: int) -> DraftVersion:
        """One kept version's full text."""
        row = await self._live(public_id, None)
        found = await self._repository.get_version(row.id, number)
        if found is None:
            raise DraftNotFoundError(_VERSION_NOT_FOUND)
        return version_from_row(found)

    async def restore_version(self, public_id: str, expected_revision: int, number: int) -> Draft:
        """Bring back a version's text as the next revision.

        The current text is saved as a version first, so the restore can itself be undone
        by restoring that version. The restored text is then saved as a "restored" version.
        Restoring text the draft already has changes nothing.
        """

        async def run(now: datetime) -> Draft:
            row = await self._live(public_id, expected_revision)
            target = await self._repository.get_version(row.id, number)
            if target is None:
                raise DraftNotFoundError(_VERSION_NOT_FOUND)
            restored = content_of(target)
            if restored == content_of(row):
                return await self._load(row)
            await self._save_version(row, DraftVersionOrigin.EDITED, now)
            apply_content(row, restored)
            _touch(row, now)
            await self._save_version(row, DraftVersionOrigin.RESTORED, now)
            return await self._load(row)

        return await self._write(run)

    async def save_as_new(self, from_public_id: str, edit: DraftEdit) -> Draft:
        """A new draft of the same kind, action link and sources, holding ``edit``.

        The editor uses it when an autosave conflicts, so the original may since have been
        deleted; it is still copied from.
        """

        async def run(now: datetime) -> Draft:
            original = await self._repository.get_draft(from_public_id, include_deleted=True)
            if original is None:
                raise DraftNotFoundError(_NOT_FOUND)
            row = await self._new_draft(
                DraftKind(original.kind),
                edit,
                now,
                action_id=original.action_id,
                action_title=original.action_title,
            )
            for source in await self._repository.source_rows(original.id):
                self._repository.copy_source(row.id, source)
            await self._session.flush()
            return await self._load(row)

        return await self._write(run)

    async def delete(self, public_id: str, expected_revision: int) -> None:
        """Hide a draft; restore() brings it back."""

        async def run(now: datetime) -> None:
            row = await self._live(public_id, expected_revision)
            row.deleted_at_utc = now
            _touch(row, now)

        await self._write(run)

    async def restore(self, public_id: str) -> Draft:
        """Bring back a deleted draft; a draft that isn't deleted is returned as it is."""

        async def run(now: datetime) -> Draft:
            row = await self._repository.get_draft(public_id, include_deleted=True)
            if row is None:
                raise DraftNotFoundError(_NOT_FOUND)
            if row.deleted_at_utc is not None:
                row.deleted_at_utc = None
                _touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def get(self, public_id: str) -> Draft:
        """A live draft; deleted drafts are not found."""
        return await self._load(await self._live(public_id, None))

    async def list_summaries(self) -> tuple[DraftSummary, ...]:
        """Every live draft, most recently updated first; the list is never capped."""
        return tuple(await self._repository.list_summaries())


def _first_available(sources: list[ActionSourceTable]) -> ActionSourceTable | None:
    return next((source for source in sources if source.message_id is not None), None)
