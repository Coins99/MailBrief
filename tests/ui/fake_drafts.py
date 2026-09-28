"""An in-memory stand-in for the runtime's draft calls, with revisions and versions."""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import HttpUrl

from mailbrief.domain.drafts import (
    MAX_DRAFT_VERSIONS,
    Draft,
    DraftEdit,
    DraftKind,
    DraftSource,
    DraftSummary,
    DraftVersion,
    DraftVersionInfo,
    DraftVersionOrigin,
    first_line,
)
from mailbrief.services.actions import ActionNotFoundError
from mailbrief.services.drafts import (
    DraftConflictError,
    DraftNotFoundError,
    SourceNotFoundError,
    reply_recipient,
    reply_title,
)

DRAFT_START = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
REPLY_SOURCE = DraftSource(
    provider_message_id="local-1",
    subject="Approval needed by Friday",
    sender_address="alex@example.com",
    web_link=HttpUrl("https://mail.google.com/mail/u/?authuser=owner%40example.com#all/abc"),
    received_at_utc=datetime(2026, 9, 4, 9, tzinfo=UTC),
    available=True,
)


class FakeDrafts:
    """Draft calls record what the window asked for.

    ``draft_fail`` makes the next draft write raise; ``draft_gate``, when set, holds every
    autosave until it is set, so tests can queue several while one is running.
    """

    def __init__(self) -> None:
        self.drafts: dict[str, Draft] = {}
        self.deleted: set[str] = set()
        self.versions: dict[str, list[DraftVersion]] = {}
        self.draft_calls: list[tuple[object, ...]] = []
        self.draft_fail: Exception | None = None
        self.draft_gate: asyncio.Event | None = None
        self.draft_list_fail: Exception | None = None
        self.draft_lists = 0
        self.reply_sources: dict[tuple[str, str], DraftSource] = {
            ("owner@example.com", "local-1"): REPLY_SOURCE
        }
        self.action_titles: dict[str, str] = {}
        self._ticks = 0

    def _now(self) -> datetime:
        self._ticks += 1
        return DRAFT_START + timedelta(minutes=self._ticks)

    async def _draft_call(self, name: str, *args: object) -> None:
        self.draft_calls.append((name, *args))
        if self.draft_fail is not None:
            failure, self.draft_fail = self.draft_fail, None
            raise failure

    def _live(self, public_id: str, revision: int | None = None) -> Draft:
        draft = self.drafts.get(public_id)
        if draft is None or public_id in self.deleted:
            raise DraftNotFoundError("That draft was not found.")
        if revision is not None and draft.revision != revision:
            raise DraftConflictError("stale")
        return draft

    def _add(self, kind: DraftKind, content: DraftEdit, **links: object) -> Draft:
        public_id = f"00000000-0000-4000-8000-{len(self.drafts) + 1:012d}"
        now = self._now()
        draft = Draft.model_validate(
            {
                **content.model_dump(),
                **links,
                "public_id": public_id,
                "kind": kind,
                "created_at_utc": now,
                "updated_at_utc": now,
                "revision": 1,
            }
        )
        self.drafts[public_id] = draft
        self.versions[public_id] = []
        self._version(public_id, DraftVersionOrigin.CREATED, content)
        return draft

    def _version(
        self, public_id: str, origin: DraftVersionOrigin, content: DraftEdit
    ) -> DraftVersionInfo | None:
        kept = self.versions[public_id]
        if kept and kept[-1].content() == content:
            return None
        number = kept[-1].number + 1 if kept else 1
        version = DraftVersion(
            number=number, origin=origin, created_at_utc=self._now(), **content.model_dump()
        )
        kept.append(version)
        del kept[:-MAX_DRAFT_VERSIONS]
        return self._info(version)

    @staticmethod
    def _info(version: DraftVersion) -> DraftVersionInfo:
        return DraftVersionInfo(
            number=version.number,
            origin=version.origin,
            created_at_utc=version.created_at_utc,
            preview=first_line(version.body) or first_line(version.title),
            length=len(version.body),
        )

    def _update(self, draft: Draft, content: DraftEdit) -> Draft:
        changed = draft.model_copy(
            update={
                **content.model_dump(),
                "revision": draft.revision + 1,
                "updated_at_utc": self._now(),
            }
        )
        self.drafts[draft.public_id] = changed
        return changed

    async def list_drafts(self) -> tuple[DraftSummary, ...]:
        self.draft_lists += 1
        if self.draft_list_fail is not None:
            raise self.draft_list_fail
        live = [d for key, d in self.drafts.items() if key not in self.deleted]
        live.sort(key=lambda d: d.updated_at_utc, reverse=True)
        return tuple(
            DraftSummary(
                public_id=d.public_id,
                kind=d.kind,
                display_title=d.display_title,
                updated_at_utc=d.updated_at_utc,
                placeholder_count=len(d.placeholders),
                action_title=d.action_title,
                source_subject=d.sources[0].subject if d.sources else None,
                revision=d.revision,
            )
            for d in live
        )

    async def get_draft(self, public_id: str) -> Draft:
        await self._draft_call("get_draft", public_id)
        return self._live(public_id)

    async def create_draft(self, kind: DraftKind) -> Draft:
        await self._draft_call("create_draft", kind)
        return self._add(kind, DraftEdit())

    async def create_reply_draft(self, account_email: str, provider_message_id: str) -> Draft:
        await self._draft_call("create_reply_draft", account_email, provider_message_id)
        source = self.reply_sources.get((account_email, provider_message_id))
        if source is None:
            raise SourceNotFoundError("That email is no longer in local mail.")
        content = DraftEdit(
            title=reply_title(source.subject), to_text=reply_recipient(source.sender_address)
        )
        return self._add(DraftKind.REPLY, content, sources=(source,))

    async def create_draft_for_action(self, public_id: str, kind: DraftKind) -> Draft:
        await self._draft_call("create_draft_for_action", public_id, kind)
        title = self.action_titles.get(public_id)
        if title is None:
            raise ActionNotFoundError("That action was not found.")
        content = (
            DraftEdit(title=reply_title(REPLY_SOURCE.subject), to_text="alex@example.com")
            if kind is DraftKind.REPLY
            else DraftEdit(title=title)
        )
        return self._add(
            kind,
            content,
            action_public_id=public_id,
            action_title=title,
            sources=(REPLY_SOURCE,),
        )

    async def autosave_draft(self, public_id: str, revision: int, edit: DraftEdit) -> Draft:
        await self._draft_call("autosave_draft", public_id, revision, edit)
        if self.draft_gate is not None:
            await self.draft_gate.wait()
        return self._update(self._live(public_id, revision), edit)

    async def checkpoint_draft(self, public_id: str, revision: int) -> DraftVersionInfo | None:
        await self._draft_call("checkpoint_draft", public_id, revision)
        draft = self._live(public_id, revision)
        return self._version(public_id, DraftVersionOrigin.EDITED, draft.content())

    async def draft_versions(self, public_id: str) -> tuple[DraftVersionInfo, ...]:
        await self._draft_call("draft_versions", public_id)
        self._live(public_id)
        return tuple(self._info(v) for v in reversed(self.versions[public_id]))

    async def draft_version(self, public_id: str, number: int) -> DraftVersion:
        await self._draft_call("draft_version", public_id, number)
        self._live(public_id)
        for version in self.versions[public_id]:
            if version.number == number:
                return version
        raise DraftNotFoundError("That draft version was not found.")

    async def restore_draft_version(self, public_id: str, revision: int, number: int) -> Draft:
        await self._draft_call("restore_draft_version", public_id, revision, number)
        draft = self._live(public_id, revision)
        target = next(v for v in self.versions[public_id] if v.number == number)
        self._version(public_id, DraftVersionOrigin.EDITED, draft.content())
        restored = self._update(draft, target.content())
        self._version(public_id, DraftVersionOrigin.RESTORED, restored.content())
        return restored

    async def save_draft_as_new(self, public_id: str, edit: DraftEdit) -> Draft:
        await self._draft_call("save_draft_as_new", public_id, edit)
        original = self.drafts[public_id]
        return self._add(
            original.kind,
            edit,
            action_public_id=original.action_public_id,
            action_title=original.action_title,
            sources=original.sources,
        )

    async def delete_draft(self, public_id: str, revision: int) -> None:
        await self._draft_call("delete_draft", public_id, revision)
        self._update(self._live(public_id, revision), self.drafts[public_id].content())
        self.deleted.add(public_id)

    async def restore_draft(self, public_id: str) -> Draft:
        await self._draft_call("restore_draft", public_id)
        self.deleted.discard(public_id)
        return self.drafts[public_id]

    def change_elsewhere(self, public_id: str, body: str = "Changed elsewhere") -> None:
        """Another window saved this draft, so the editor's revision is stale."""
        draft = self.drafts[public_id]
        self._update(draft, draft.content().model_copy(update={"body": body}))
