"""AI drafting: parts, preview, consent, the saved own text, retries, validation and apply."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import ActionEdit, ActionStatus
from mailbrief.domain.analysis import ActionOwnership
from mailbrief.domain.drafting import (
    DraftCandidate,
    DraftContextPart,
    DraftingOptions,
    DraftingProblem,
    DraftingRequest,
    DraftingResponse,
    DraftingStatus,
    part_sizes,
)
from mailbrief.domain.drafts import (
    DraftEdit,
    DraftKind,
    DraftLength,
    DraftTone,
    DraftVersionOrigin,
)
from mailbrief.domain.messages import AccountIdentity, EmailContact, ProviderKind
from mailbrief.ports.errors import (
    AIAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderUsageLimitError,
)
from mailbrief.services.actions import ActionService
from mailbrief.services.bodies import BodyService
from mailbrief.services.drafting import (
    DRAFTING_DISCLOSURE_VERSION,
    DRAFTING_SCOPE,
    DraftingContextError,
    DraftingService,
    disclosure_lines,
    validate_generated,
)
from mailbrief.services.drafts import DraftService
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    OwnerConsentRepository,
)
from mailbrief.storage.tables import ActionTable, DraftGenerationTable, OwnerConsentTable
from tests.factories import make_message
from tests.unit.services.drafting_fakes import FakeBodies, FakeDraftingProvider, Gate, answer

START = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
TORONTO = ZoneInfo("America/Toronto")
OWNER = "me@x.com"
MARKER = "BODY-MARKER-7f3a"
EMAIL_BODY = (
    f"Hi,\n\nCould you send the Q3 numbers by Friday? {MARKER}\n\n"
    "On Mon, Sep 28, 2026 at 9:00 AM Sam <sam@example.com> wrote:\n> Earlier thread text\n"
)


@pytest.fixture(autouse=True)
def scripts_are_used_up() -> Any:
    FakeDraftingProvider.created.clear()
    yield
    leftovers = [len(p.script) for p in FakeDraftingProvider.created if p.script]
    FakeDraftingProvider.created.clear()
    assert not leftovers, f"unused drafting script items: {leftovers}"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "drafting.sqlite3"


@pytest.fixture
async def database(database_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(database_path)
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


@pytest.fixture
def bodies() -> FakeBodies:
    return FakeBodies(EMAIL_BODY)


def service(
    session: AsyncSession,
    provider: FakeDraftingProvider,
    bodies: FakeBodies | None,
    *,
    limit: int = 4_000,
) -> DraftingService:
    return DraftingService(
        session,
        provider,
        None if bodies is None else BodyService(bodies, limit=limit),
        clock=Clock(),
        zone=TORONTO,
    )


async def seed_reply(session: AsyncSession) -> str:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL, provider_account_id="gmail-1", email_address=OWNER
        )
    )
    await MessageRepository(session).upsert_messages(
        account.id,
        [
            make_message(
                provider=ProviderKind.GMAIL,
                provider_account_id="gmail-1",
                provider_message_id="msg-1",
                subject="Q3 numbers",
                sender=EmailContact(name="Alex Doe", address="alex@example.com"),
                received_at_utc=datetime(2026, 9, 28, 13, 30, tzinfo=UTC),
                web_link="https://mail.google.com/mail/u/0/#all/msg-1",
            )
        ],
    )
    await session.commit()
    draft = await DraftService(session).create_reply(OWNER, "msg-1")
    return draft.public_id


async def seed_action_note(session: AsyncSession, notes: str = "Ask Sam first.") -> str:
    row = await ActionRepository(session).add_action(
        ActionTable(
            public_id="11111111-1111-4111-8111-111111111111",
            title="Send the Q3 numbers",
            ownership="mine",
            status=ActionStatus.OPEN.value,
            deadline_precision="unresolved",
            deadline_text="by Friday",
            notes="",
            created_at_utc=START,
            updated_at_utc=START,
            revision=1,
        )
    )
    ActionRepository(session).add_step(row.id, 0, "Collect the figures", None)
    await session.commit()
    await ActionService(session).save(
        row.public_id,
        1,
        ActionEdit(
            title="Send the Q3 numbers",
            ownership=ActionOwnership.MINE,
            effort=None,
            target_date=datetime(2026, 10, 1).date(),
            notes=notes,
        ),
    )
    draft = await DraftService(session).create_for_action(row.public_id, DraftKind.NOTE)
    return draft.public_id


OPTIONS = DraftingOptions(
    parts=frozenset({DraftContextPart.SOURCE_EMAIL, DraftContextPart.CURRENT_TEXT}),
    tone=DraftTone.WARM,
    length=DraftLength.SHORT,
    instructions="Say yes.",
)


async def count(session: AsyncSession, table: type[Any]) -> int:
    return (await session.scalar(select(func.count()).select_from(table))) or 0


# Parts and preparation


async def test_available_parts(session: AsyncSession, bodies: FakeBodies) -> None:
    reply = await seed_reply(session)
    note = await seed_action_note(session)
    blank = (await DraftService(session).create(DraftKind.MESSAGE)).public_id
    provider = FakeDraftingProvider()

    assert await service(session, provider, bodies).available_parts(reply) == {
        DraftContextPart.SOURCE_EMAIL,
        DraftContextPart.CURRENT_TEXT,
    }
    assert await service(session, provider, None).available_parts(reply) == {
        DraftContextPart.CURRENT_TEXT
    }
    assert await service(session, provider, bodies).available_parts(note) == {
        DraftContextPart.ACTION,
        DraftContextPart.CURRENT_TEXT,
    }
    assert await service(session, provider, bodies).available_parts(blank) == frozenset()

    await ActionService(session).delete("11111111-1111-4111-8111-111111111111", 2)
    assert DraftContextPart.ACTION not in await service(session, provider, bodies).available_parts(
        note
    )


async def test_prepare_downloads_and_prepares_the_email_now(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    public_id = await seed_reply(session)
    assert bodies.fetched == []

    plan = await service(session, FakeDraftingProvider(), bodies).prepare(public_id, OPTIONS)

    assert bodies.fetched == ["msg-1"]
    source = plan.request.source
    assert source is not None
    assert source.subject == "Q3 numbers"
    assert source.sender_name == "Alex Doe"
    assert source.received_local == "2026-09-28T09:30-04:00"
    assert "Earlier thread text" not in source.body  # Quoted history is trimmed.
    assert MARKER in source.body
    assert plan.request.current is not None
    assert plan.request.current.title == "Re: Q3 numbers"
    assert plan.request.action is None
    assert "@" not in plan.request.model_dump_json()  # No address, ever.
    assert (plan.request.kind, plan.request.tone, plan.request.length) == (
        DraftKind.REPLY,
        DraftTone.WARM,
        DraftLength.SHORT,
    )
    assert plan.request.today == datetime(2026, 9, 28).date()
    assert plan.revision == 1


async def test_the_preview_shows_each_chosen_part_and_its_exact_size(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    public_id = await seed_reply(session)

    plan = await service(session, FakeDraftingProvider(), bodies).prepare(public_id, OPTIONS)

    sizes = part_sizes(plan.request)
    preview = plan.preview
    assert (preview.provider, preview.model, preview.first_use) == ("groq", "fake-model", True)
    assert [(line.label.split(":")[0], line.characters) for line in preview.lines] == [
        ("Always", sizes[None]),
        ("The email", sizes[DraftContextPart.SOURCE_EMAIL]),
        ("Your current text", sizes[DraftContextPart.CURRENT_TEXT]),
    ]
    assert preview.total_characters == sum(sizes.values())


async def test_prepare_caps_action_notes_and_current_text(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    public_id = await seed_action_note(session, notes="word " * 1_000)
    await DraftService(session).autosave(public_id, 1, DraftEdit(title="T", body="x " * 5_000))
    options = DraftingOptions(
        parts=frozenset({DraftContextPart.ACTION, DraftContextPart.CURRENT_TEXT})
    )

    plan = await service(session, FakeDraftingProvider(), bodies).prepare(public_id, options)

    action, current = plan.request.action, plan.request.current
    assert action is not None and current is not None
    assert (action.title, action.ownership, action.deadline_text) == (
        "Send the Q3 numbers",
        ActionOwnership.MINE,
        "by Friday",
    )
    assert action.steps == ("Collect the figures",)
    assert action.target_date is not None
    assert 1_600 <= len(action.notes) <= 2_000
    assert len(current.body) <= 8_000
    assert plan.preview.lines[-1].label.endswith("(cut to fit)")
    assert bodies.fetched == []  # The email wasn't chosen, so nothing was downloaded.


async def test_prepare_refuses_parts_that_are_not_available(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    public_id = await seed_reply(session)
    drafting = service(session, FakeDraftingProvider(), bodies)

    with pytest.raises(DraftingContextError, match="no longer available"):
        await drafting.prepare(
            public_id, DraftingOptions(parts=frozenset({DraftContextPart.ACTION}))
        )
    with pytest.raises(DraftingContextError):
        await service(session, FakeDraftingProvider(), None).prepare(public_id, OPTIONS)


@pytest.mark.parametrize(("text", "missing"), [("", False), ("text", True)])
async def test_an_email_that_cant_be_downloaded_stops_prepare(
    session: AsyncSession, text: str, missing: bool
) -> None:
    public_id = await seed_reply(session)
    reader = FakeBodies(text, missing=missing)

    with pytest.raises(DraftingContextError, match="couldn't be downloaded"):
        await service(session, FakeDraftingProvider(), reader).prepare(public_id, OPTIONS)


# Generation


async def prepared(
    session: AsyncSession, provider: FakeDraftingProvider, bodies: FakeBodies
) -> Any:
    public_id = await seed_reply(session)
    drafting = service(session, provider, bodies)
    return public_id, drafting, await drafting.prepare(public_id, OPTIONS)


async def test_first_use_asks_records_consent_before_sending_and_later_runs_do_not_ask_again(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    seen_consent: list[bool] = []

    def respond(request: DraftingRequest) -> DraftingResponse:
        seen_consent.append(True)
        return answer()

    provider = FakeDraftingProvider([respond, answer()])
    public_id, drafting, plan = await prepared(session, provider, bodies)
    gate = Gate()

    outcome = await drafting.generate(plan, gate)

    assert outcome.status is DraftingStatus.GENERATED
    assert gate.previews[0].first_use is True
    consent = await OwnerConsentRepository(session).get_active(
        "groq", DRAFTING_SCOPE, DRAFTING_DISCLOSURE_VERSION
    )
    assert consent is not None and consent.granted_at_utc <= START
    second = await drafting.prepare(public_id, OPTIONS)
    assert second.preview.first_use is False
    await drafting.generate(second, gate)
    assert gate.previews[1].first_use is False
    assert await count(session, OwnerConsentTable) == 1


async def test_consent_is_recorded_before_the_call(
    session: AsyncSession, database: Database, bodies: FakeBodies
) -> None:
    active_at_call: list[bool] = []

    async def check() -> bool:
        async with database.session() as other:
            consent = await OwnerConsentRepository(other).get_active(
                "groq", DRAFTING_SCOPE, DRAFTING_DISCLOSURE_VERSION
            )
            return consent is not None

    class Checking(FakeDraftingProvider):
        async def draft(self, request: DraftingRequest) -> DraftingResponse:
            active_at_call.append(await check())
            return await super().draft(request)

    provider = Checking([answer()])
    _, drafting, plan = await prepared(session, provider, bodies)

    await drafting.generate(plan, Gate())

    assert active_at_call == [True]


async def test_declining_sends_nothing_and_changes_nothing(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider()
    public_id, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate(approve=False))

    assert outcome.status is DraftingStatus.DECLINED
    assert provider.requests == []
    assert await count(session, OwnerConsentTable) == 0
    assert len(await DraftService(session).versions(public_id)) == 1


async def test_after_revoking_the_next_generation_asks_again(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider([answer(), answer()])
    public_id, drafting, plan = await prepared(session, provider, bodies)
    gate = Gate()
    await drafting.generate(plan, gate)

    assert await OwnerConsentRepository(session).revoke_all("groq", START) == 1
    await session.commit()
    again = await drafting.prepare(public_id, OPTIONS)
    await drafting.generate(again, gate)

    assert [preview.first_use for preview in gate.previews] == [True, True]


async def test_a_missing_key_fails_before_asking(session: AsyncSession, bodies: FakeBodies) -> None:
    provider = FakeDraftingProvider(credentials=False)
    _, drafting, plan = await prepared(session, provider, bodies)
    gate = Gate()

    outcome = await drafting.generate(plan, gate)

    assert (outcome.status, outcome.error_code) == (DraftingStatus.FAILED, "AI_KEY_MISSING")
    assert gate.previews == []


async def test_the_owners_text_is_saved_as_a_version_before_the_call(
    session: AsyncSession, database: Database, bodies: FakeBodies
) -> None:
    public_id = await seed_reply(session)
    await DraftService(session).autosave(
        public_id, 1, DraftEdit(title="Re: Q3 numbers", to_text="alex@example.com", body="Mine")
    )
    versions_at_call: list[list[str]] = []

    class Checking(FakeDraftingProvider):
        async def draft(self, request: DraftingRequest) -> DraftingResponse:
            async with database.session() as other:
                versions = await DraftService(other).versions(public_id)
                texts = [
                    (await DraftService(other).version(public_id, v.number)).body for v in versions
                ]
            versions_at_call.append(texts)
            return await super().draft(request)

    drafting = service(session, Checking([answer("Generated reply")]), bodies)
    plan = await drafting.prepare(public_id, OPTIONS)

    outcome = await drafting.generate(plan, Gate())

    assert versions_at_call == [["Mine", ""]]
    assert (outcome.previous_version, outcome.version_number) == (2, 3)
    assert outcome.draft is not None and outcome.draft.body == "Generated reply"
    assert outcome.draft.to_text == "alex@example.com"  # Recipients are never touched.
    assert outcome.draft.revision == 3


async def test_an_unchanged_text_points_at_its_existing_version(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider([answer()])
    _, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate())

    assert (outcome.previous_version, outcome.version_number) == (1, 2)


async def test_a_draft_changed_since_prepare_stops_before_sending(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider()
    public_id, drafting, plan = await prepared(session, provider, bodies)
    await DraftService(session).autosave(public_id, 1, DraftEdit(title="Changed"))

    outcome = await drafting.generate(plan, Gate())

    assert (outcome.status, outcome.error_code) == (DraftingStatus.FAILED, "DRAFT_CHANGED")
    assert provider.requests == []


async def test_a_draft_changed_during_the_call_is_not_overwritten(
    session: AsyncSession, database: Database, bodies: FakeBodies
) -> None:
    public_id = await seed_reply(session)

    def change_then_answer(request: DraftingRequest) -> DraftingResponse:
        return answer("AI text")

    class Racing(FakeDraftingProvider):
        async def draft(self, request: DraftingRequest) -> DraftingResponse:
            async with database.session() as other:
                await DraftService(other).autosave(public_id, 1, DraftEdit(title="Typed meanwhile"))
            return await super().draft(request)

    drafting = service(session, Racing([change_then_answer]), bodies)
    plan = await drafting.prepare(public_id, OPTIONS)

    outcome = await drafting.generate(plan, Gate())

    assert outcome.error_code == "DRAFT_CHANGED"
    after = await DraftService(session).get(public_id)
    assert (after.title, after.body) == ("Typed meanwhile", "")


@pytest.mark.parametrize(
    ("first", "second", "status", "code", "calls"),
    [
        (DraftingProblem.INCOMPLETE, None, DraftingStatus.GENERATED, None, 2),
        (
            DraftingProblem.INCOMPLETE,
            DraftingProblem.INCOMPLETE,
            DraftingStatus.FAILED,
            "AI_OUTPUT_INCOMPLETE",
            2,
        ),
        (
            DraftingProblem.INVALID_OUTPUT,
            DraftingProblem.INVALID_OUTPUT,
            DraftingStatus.FAILED,
            "AI_INVALID_OUTPUT",
            2,
        ),
        (
            DraftingProblem.INCOMPLETE,
            DraftingProblem.REFUSED,
            DraftingStatus.FAILED,
            "AI_INVALID_OUTPUT",
            2,
        ),
        (
            DraftingProblem.INVALID_OUTPUT,
            DraftingProblem.INCOMPLETE,
            DraftingStatus.FAILED,
            "AI_OUTPUT_INCOMPLETE",
            2,
        ),
    ],
)
async def test_an_unusable_answer_is_tried_once_more(
    session: AsyncSession,
    bodies: FakeBodies,
    first: DraftingProblem,
    second: DraftingProblem | None,
    status: DraftingStatus,
    code: str | None,
    calls: int,
) -> None:
    script = [
        DraftingResponse(problem=first),
        answer() if second is None else DraftingResponse(problem=second),
    ]
    provider = FakeDraftingProvider(script)
    public_id, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate())

    assert (outcome.status, outcome.error_code) == (status, code)
    assert len(provider.requests) == calls
    if status is DraftingStatus.FAILED:
        assert (await DraftService(session).get(public_id)).body == ""


async def test_an_answer_that_copies_the_email_is_retried(session: AsyncSession) -> None:
    long_email = " ".join(f"Point {index} of the plan needs a reply." for index in range(20))
    copied = answer("Sure. " + long_email[:250])
    provider = FakeDraftingProvider([copied, answer("My own words.")])
    _, drafting, plan = await prepared(session, provider, FakeBodies(long_email))

    outcome = await drafting.generate(plan, Gate())

    assert outcome.status is DraftingStatus.GENERATED
    assert outcome.draft is not None and outcome.draft.body == "My own words."


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (
            AIAuthenticationError("no", http_status=401, provider_error_code="invalid_api_key"),
            "AI_AUTH_FAILED",
            "HTTP 401, code invalid_api_key",
        ),
        (ProviderRateLimitError("slow"), "AI_RATE_LIMITED", None),
        (ProviderError("offline"), "AI_NETWORK_ERROR", None),
        (ProviderRequestRejectedError("no", http_status=400), "AI_REQUEST_REJECTED", "HTTP 400"),
        (ProviderUsageLimitError("budget"), "AI_USAGE_LIMIT", None),
    ],
)
async def test_provider_errors_leave_the_text_unchanged(
    session: AsyncSession,
    bodies: FakeBodies,
    error: Exception,
    code: str,
    detail: str | None,
) -> None:
    provider = FakeDraftingProvider([error])
    public_id, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate())

    assert (outcome.status, outcome.error_code, outcome.provider_detail) == (
        DraftingStatus.FAILED,
        code,
        detail,
    )
    draft = await DraftService(session).get(public_id)
    assert (draft.body, draft.revision) == ("", 1)
    assert [v.origin for v in await DraftService(session).versions(public_id)] == [
        DraftVersionOrigin.CREATED
    ]


async def test_cancel_before_or_during_the_call_writes_nothing(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    cancel = asyncio.Event()

    def cancel_then_answer(request: DraftingRequest) -> DraftingResponse:
        cancel.set()
        return answer("Too late")

    provider = FakeDraftingProvider([cancel_then_answer])
    public_id, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate(), cancel)

    assert outcome.status is DraftingStatus.CANCELLED
    assert (await DraftService(session).get(public_id)).body == ""
    assert await count(session, DraftGenerationTable) == 0
    assert (await drafting.generate(plan, Gate(), cancel)).status is DraftingStatus.CANCELLED
    assert len(provider.requests) == 1


async def test_cancel_after_approval_sends_nothing(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    cancel = asyncio.Event()

    class CancellingGate(Gate):
        async def request_drafting_consent(self, preview: Any) -> bool:
            cancel.set()
            return True

    provider = FakeDraftingProvider()
    _, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, CancellingGate(), cancel)

    assert outcome.status is DraftingStatus.CANCELLED
    assert provider.requests == []


async def test_a_generation_becomes_a_generated_version_with_its_record(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider([answer("Hello\x1b[31m there", missing=["a", "b"])])
    public_id, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate())

    assert outcome.missing_context == ("a", "b")
    newest = (await DraftService(session).versions(public_id))[0]
    assert newest.origin is DraftVersionOrigin.GENERATED
    assert newest.generation is not None
    assert (newest.generation.tone, newest.generation.length, newest.generation.model) == (
        DraftTone.WARM,
        DraftLength.SHORT,
        "fake-model",
    )
    record = await session.scalar(select(DraftGenerationTable))
    assert record is not None
    assert (record.provider, record.prompt_version, record.instructions) == (
        "groq",
        "fake-draft-1",
        "Say yes.",
    )
    assert record.parts_json == ["current_text", "source_email"]
    assert record.missing_context_json == ["a", "b"]
    assert (await DraftService(session).get(public_id)).body == "Hello[31m there"


async def test_the_email_body_is_never_stored(
    session: AsyncSession, database_path: Path, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider([lambda request: answer("A reply in my own words.")])
    _, drafting, plan = await prepared(session, provider, bodies)
    assert plan.request.source is not None and MARKER in plan.request.source.body

    await drafting.generate(plan, Gate())
    await session.commit()
    await session.close()

    stored = b"".join(
        path.read_bytes()
        for path in database_path.parent.iterdir()
        if path.name.startswith(database_path.name)
    )
    assert MARKER.encode() not in stored
    assert b"Could you send the Q3 numbers" not in stored


async def test_generated_records_go_when_their_versions_are_pruned(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    provider = FakeDraftingProvider([answer("AI text")])
    public_id, drafting, plan = await prepared(session, provider, bodies)
    outcome = await drafting.generate(plan, Gate())
    assert outcome.draft is not None
    drafts = DraftService(session)
    revision = outcome.draft.revision
    for index in range(100):
        revision = (
            await drafts.autosave(public_id, revision, DraftEdit(body=f"v{index}"))
        ).revision
        await drafts.checkpoint(public_id, revision)

    assert await count(session, DraftGenerationTable) == 0


# Validation


def request(source_body: str | None = None) -> DraftingRequest:
    from mailbrief.domain.drafting import SourceContext

    return DraftingRequest(
        kind=DraftKind.REPLY,
        tone=DraftTone.NEUTRAL,
        length=DraftLength.MEDIUM,
        today=START.date(),
        source=None
        if source_body is None
        else SourceContext(
            received_local="2026-09-28T09:30-04:00", body=source_body, body_truncated=False
        ),
    )


@pytest.mark.parametrize(
    ("kind", "subject", "expected"),
    [
        (DraftKind.EMAIL, "  Numbers‮ for Q3 ", "Numbers for Q3"),
        (DraftKind.NOTE, "Plan", "Plan"),
        (DraftKind.REPLY, "Re: ignored", None),
        (DraftKind.MESSAGE, "ignored", None),
        (DraftKind.EMAIL, "   ", None),
        (DraftKind.EMAIL, None, None),
    ],
)
def test_subjects_only_for_emails_and_notes(
    kind: DraftKind, subject: str | None, expected: str | None
) -> None:
    candidate = DraftCandidate(subject=subject, body="Body")
    assert validate_generated(candidate, request(), kind).subject == expected


def test_a_long_subject_is_cut_at_a_word() -> None:
    candidate = DraftCandidate(subject="word " * 60, body="Body")
    subject = validate_generated(candidate, request(), DraftKind.EMAIL).subject
    assert subject is not None and len(subject) <= 200 and subject.endswith("word")


def test_the_body_is_cleaned_and_loses_quoted_history() -> None:
    body = (
        "Thanks Alex,\r\n\r\n\r\n\r\nWill do.  \n\n"
        "On Mon, Sep 28, 2026 at 9:00 AM Alex <alex@example.com> wrote:\n> Please send it\n"
    )
    generated = validate_generated(DraftCandidate(body=body), request(), DraftKind.REPLY)
    assert generated.body == "Thanks Alex,\n\n\nWill do."


@pytest.mark.parametrize("body", ["", " \n\t ", "x" * 8_001])
def test_empty_or_long_bodies_are_invalid(body: str) -> None:
    with pytest.raises(ValueError):
        validate_generated(DraftCandidate(body=body), request(), DraftKind.REPLY)


def test_the_copy_guard_rejects_200_copied_characters() -> None:
    source = "".join(chr(ord("a") + (index * 11) % 26) for index in range(300))
    ok = validate_generated(
        DraftCandidate(body="Mine: " + source[:199]), request(source), DraftKind.REPLY
    )
    assert ok.body.startswith("Mine")
    with pytest.raises(ValueError):
        validate_generated(
            DraftCandidate(body="Mine: " + source[:200].upper()), request(source), DraftKind.REPLY
        )


def test_missing_context_is_cleaned_and_bounded() -> None:
    items = ("  the date ", "", "\x1b", "x" * 300, "four", "five", "six", "seven")
    generated = validate_generated(
        DraftCandidate(body="Body", missing_context=items), request(), DraftKind.REPLY
    )
    assert generated.missing_context[:1] == ("the date",)
    assert len(generated.missing_context) == 5
    assert all(len(item) <= 200 for item in generated.missing_context)


def test_disclosure_lines() -> None:
    from mailbrief.domain.drafting import DraftingPreview, PreviewLine

    preview = DraftingPreview(
        provider="groq",
        model="m",
        privacy_notice="Enable Zero Data Retention.",
        first_use=True,
        lines=(PreviewLine(label="Your current text: title and body", characters=1_234),),
    )
    lines = disclosure_lines(preview)
    assert lines[0] == "MailBrief will send these parts to Groq (m) to write this draft:"
    assert "- Your current text: title and body: 1,234 characters" in lines
    assert any("never sends email addresses or recipients" in line for line in lines)
    assert "Enable Zero Data Retention." in lines
    assert "revoke" in lines[-1]
    assert "revoke" not in disclosure_lines(preview.model_copy(update={"first_use": False}))[-1]


async def test_today_is_the_owners_local_date(session: AsyncSession, bodies: FakeBodies) -> None:
    public_id = await seed_reply(session)
    late = Clock()
    late.now = START + timedelta(hours=14, minutes=30)  # 03:30 UTC: the 28th in Toronto.
    drafting = DraftingService(
        session, FakeDraftingProvider(), BodyService(bodies), clock=late, zone=TORONTO
    )

    plan = await drafting.prepare(public_id, OPTIONS)

    assert plan.request.today == datetime(2026, 9, 28).date()


async def test_cancel_between_attempts_stops_the_retry(
    session: AsyncSession, bodies: FakeBodies
) -> None:
    cancel = asyncio.Event()

    def cancel_and_fail(request: DraftingRequest) -> DraftingResponse:
        cancel.set()
        return DraftingResponse(problem=DraftingProblem.INCOMPLETE)

    provider = FakeDraftingProvider([cancel_and_fail])
    _, drafting, plan = await prepared(session, provider, bodies)

    outcome = await drafting.generate(plan, Gate(), cancel)

    assert outcome.status is DraftingStatus.CANCELLED
    assert len(provider.requests) == 1


async def test_the_default_clock_is_real(session: AsyncSession, bodies: FakeBodies) -> None:
    public_id = await seed_reply(session)
    drafting = DraftingService(session, FakeDraftingProvider(), BodyService(bodies), zone=TORONTO)

    plan = await drafting.prepare(public_id, OPTIONS)

    assert plan.request.today == datetime.now(TORONTO).date()
