"""Brief service: consent before sending, caching, failures, cancellation and leak safety."""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import AIUsage, AnalysisRequest, AnalysisResponse
from mailbrief.domain.bodies import MAX_ANALYSIS_CHARS, BodySource, MessageBody
from mailbrief.domain.briefs import SENT_FIELDS, BriefStatus, TransmissionPreview
from mailbrief.domain.digests import DigestSection, DigestStatus, SyncProgress, SyncStage
from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    MessagePage,
    NormalizedMessage,
    ProviderKind,
    RankedMessage,
)
from mailbrief.ports.errors import AIAuthenticationError, ProviderPermissionError
from mailbrief.services.analysis import AnalysisPlan, AnalysisRun, AnalysisService
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import CONSENT_DISCLOSURE_VERSION, BriefService, disclosure_lines
from mailbrief.services.digest import DigestService
from mailbrief.services.history import BriefDateError, coverage_line
from mailbrief.services.proposals import ProposalService
from mailbrief.services.ranking import ExcludedSenderError, ShortlistReviewError
from mailbrief.services.threads import ThreadCheck, ThreadService
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    AnalysisRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import AccountTable, ActionSourceTable, ActionTable, SyncRunTable
from tests.factories import make_message
from tests.unit.services.ai_fakes import FakeAIProvider, ScriptItem, answer_all
from tests.unit.services.test_application import RecordingThreads
from tests.unit.services.test_sync import FakeEmailProvider

NOW = datetime(2026, 9, 4, 16, 0, tzinfo=UTC)  # Noon in Toronto.
TODAY = date(2026, 9, 4)
ZONE = "America/Toronto"
BODY = "Please approve the quarterly budget by Friday. The finance team is waiting on it."
MARKER = "PRIVATE-BODY-MARKER-9c41"


class FakeBodyReader:
    """Returns stored text per message ID; an unknown ID has no readable body."""

    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        text = self.texts.get(provider_message_id, "")
        source = BodySource.PLAIN if text else BodySource.NONE
        return MessageBody(provider_message_id=provider_message_id, text=text, source=source)


@dataclass
class RecordingGate:
    """Consent gate that answers fixed and records what the provider had done when asked."""

    answer: bool
    provider: FakeAIProvider | None = None
    previews: list[TransmissionPreview] = field(default_factory=list)
    calls_before_decision: list[int] = field(default_factory=list)

    async def confirm(self, preview: TransmissionPreview) -> bool:
        self.previews.append(preview)
        if self.provider is not None:
            self.calls_before_decision.append(self.provider.calls)
        return self.answer


def inbox(count: int = 2) -> list[NormalizedMessage]:
    return [
        make_message(
            provider_message_id=f"m{index}",
            subject=f"Budget {index}",
            received_at_utc=datetime(2026, 9, 4, 13, index, tzinfo=UTC),
            web_link=f"https://mail.example.com/m{index}",
        )
        for index in range(count)
    ]


def texts_for(messages: Sequence[NormalizedMessage], extra: str = "") -> dict[str, str]:
    return {
        message.provider_message_id: f"{BODY} Reference {message.provider_message_id}.{extra}"
        for message in messages
    }


def database_bytes(directory: Path, name: str) -> bytes:
    """Every byte SQLite wrote for the database, including WAL and shared-memory files."""
    return b"".join(item.read_bytes() for item in sorted(directory.glob(f"{name}*")))


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


def build(
    session: AsyncSession,
    provider: FakeAIProvider,
    gate: RecordingGate,
    *,
    messages: Sequence[NormalizedMessage] | None = None,
    texts: dict[str, str] | None = None,
    email: FakeEmailProvider | None = None,
    batch_size: int = 5,
    body_limit: int = MAX_ANALYSIS_CHARS,
    proposals: ProposalService | None = None,
    threads: ThreadService | None = None,
) -> BriefService:
    mailbox = list(inbox() if messages is None else messages)
    email = email or FakeEmailProvider(pages=[mailbox])
    application = ApplicationService(
        provider=email,
        message_repo=MessageRepository(session),
        sync_run_repo=SyncRunRepository(session),
        account_repo=AccountRepository(session),
        threads=threads,
    )
    gate.provider = provider
    return BriefService(
        session=session,
        application=application,
        bodies=BodyService(
            FakeBodyReader(texts_for(mailbox) if texts is None else texts), limit=body_limit
        ),
        analysis=AnalysisService(session, provider, batch_size=batch_size),
        digests=DigestService(session),
        consent_gate=gate,
        clock=lambda: NOW,
        proposals=proposals,
    )


async def account_id_of(session: AsyncSession) -> int:
    account = await AccountRepository(session).get_by_email("user@example.com")
    assert account is not None
    return account.id


async def test_first_run_records_consent_before_sending_and_saves(session: AsyncSession) -> None:
    provider = FakeAIProvider([answer_all(AIUsage(input_tokens=300, output_tokens=40))])
    gate = RecordingGate(answer=True)

    result = await build(session, provider, gate).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert gate.calls_before_decision == [0]
    (preview,) = gate.previews
    assert (preview.message_count, preview.first_use, preview.reused_count) == (2, True, 0)
    assert provider.calls == 1
    consent = await ConsentRepository(session).get_active(
        await account_id_of(session), "fake", CONSENT_DISCLOSURE_VERSION
    )
    assert consent is not None
    assert consent.granted_at_utc == NOW
    assert result.digest is not None
    assert result.digest.local_date == TODAY
    assert len(result.digest.items) == 2
    assert result.coverage is not None
    assert (result.coverage.analyzed, result.coverage.input_tokens) == (2, 300)
    assert (result.coverage.ai_provider, result.coverage.ai_model) == ("fake", "fake-model")


async def test_an_unchanged_second_run_reuses_everything_without_asking(
    session: AsyncSession,
) -> None:
    await build(session, FakeAIProvider([answer_all()]), RecordingGate(answer=True)).generate(
        tz_key=ZONE
    )
    provider = FakeAIProvider()
    gate = RecordingGate(answer=True)

    result = await build(session, provider, gate).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert gate.previews == []
    assert provider.calls == result.ai_calls == 0
    assert result.coverage is not None
    assert (result.coverage.reused, result.coverage.analyzed) == (2, 0)
    assert result.coverage.input_tokens is None
    assert result.coverage.ai_provider == "fake"


async def test_migration_requires_new_consent_and_keeps_provider_caches_separate(
    session: AsyncSession,
) -> None:
    class PreviousProvider(FakeAIProvider):
        @property
        def provider_name(self) -> str:
            return "openai"

    class NewProvider(FakeAIProvider):
        @property
        def provider_name(self) -> str:
            return "groq"

    old = await build(session, PreviousProvider([answer_all()]), RecordingGate(True)).generate(
        tz_key=ZONE
    )
    assert old.digest is not None
    account_id = await account_id_of(session)
    gate = RecordingGate(False)
    provider = NewProvider()
    declined = await build(session, provider, gate).generate(tz_key=ZONE)
    assert declined.status is BriefStatus.CONSENT_DECLINED
    assert provider.calls == 0
    assert gate.previews[0].first_use
    assert gate.previews[0].reused_count == 0
    historical = await DigestRepository(session).get_by_account_and_date(account_id, TODAY)
    assert historical is not None
    assert historical.ai_provider == "openai"

    fresh = await build(session, NewProvider([answer_all()]), RecordingGate(True)).generate(
        tz_key=ZONE
    )
    assert fresh.coverage is not None
    assert (fresh.coverage.analyzed, fresh.coverage.reused) == (2, 0)
    assert (
        await ConsentRepository(session).get_active(
            account_id, "openai", CONSENT_DISCLOSURE_VERSION
        )
        is not None
    )
    # Same model/prompt/input still selects the original provider's cached rows.
    reused = await build(session, PreviousProvider(), RecordingGate(False)).generate(tz_key=ZONE)
    assert reused.coverage is not None
    assert (reused.coverage.analyzed, reused.coverage.reused) == (0, 2)


async def test_a_declined_gate_sends_nothing_and_saves_nothing(session: AsyncSession) -> None:
    provider = FakeAIProvider()
    gate = RecordingGate(answer=False)

    result = await build(session, provider, gate).generate(tz_key=ZONE)

    account_id = await account_id_of(session)
    assert result.status is BriefStatus.CONSENT_DECLINED
    assert result.digest is None
    assert gate.calls_before_decision == [0]
    assert provider.calls == 0
    assert await ConsentRepository(session).get_active(account_id, "fake", "1") is None
    assert await DigestRepository(session).get_by_account_and_date(account_id, TODAY) is None


async def test_a_later_run_asks_again_without_recording_new_consent(
    session: AsyncSession,
) -> None:
    await build(session, FakeAIProvider([answer_all()]), RecordingGate(answer=True)).generate(
        tz_key=ZONE
    )
    gate = RecordingGate(answer=True)
    messages = inbox(3)

    result = await build(session, FakeAIProvider([answer_all()]), gate, messages=messages).generate(
        tz_key=ZONE
    )

    (preview,) = gate.previews
    assert (preview.message_count, preview.reused_count, preview.first_use) == (1, 2, False)
    assert result.coverage is not None
    assert (result.coverage.analyzed, result.coverage.reused) == (1, 2)


async def test_a_failed_sync_saves_nothing(session: AsyncSession) -> None:
    email = FakeEmailProvider(pages=[inbox()])
    email.fail_at_page = 1
    email.failure_exception = ProviderPermissionError("denied")
    gate = RecordingGate(answer=True)

    result = await build(session, FakeAIProvider(), gate, email=email).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SYNC_FAILED
    assert result.error_code is not None
    assert result.error_code == result.sync.error_code
    assert gate.previews == []
    account_id = await account_id_of(session)
    assert await DigestRepository(session).get_by_account_and_date(account_id, TODAY) is None


async def test_a_cancelled_sync_saves_nothing(session: AsyncSession) -> None:
    cancel = asyncio.Event()
    cancel.set()
    gate = RecordingGate(answer=True)

    result = await build(session, FakeAIProvider(), gate).generate(tz_key=ZONE, cancel=cancel)

    assert result.status is BriefStatus.CANCELLED
    assert gate.previews == []
    account_id = await account_id_of(session)
    assert await DigestRepository(session).get_by_account_and_date(account_id, TODAY) is None


async def test_a_total_analysis_failure_keeps_the_earlier_brief(session: AsyncSession) -> None:
    messages = inbox()
    first = await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(answer=True), messages=messages
    ).generate(tz_key=ZONE)
    provider = FakeAIProvider([AIAuthenticationError("bad key")])

    result = await build(
        session,
        provider,
        RecordingGate(answer=True),
        messages=messages,
        texts=texts_for(messages, extra=" Updated."),
    ).generate(tz_key=ZONE)

    assert result.status is BriefStatus.ANALYSIS_FAILED
    assert result.error_code == "AI_AUTH_FAILED"
    assert result.ai_calls == 1
    assert result.coverage is not None
    assert result.coverage.failed == 2
    assert result.coverage.ai_provider is None
    repo = DigestRepository(session)
    row = await repo.get_by_account_and_date(await account_id_of(session), TODAY)
    assert row is not None
    assert first.digest == DigestRepository.to_domain(
        row, await repo.get_digest_items(row.id), "user@example.com"
    )


async def test_a_partial_brief_reports_the_provider_error(session: AsyncSession) -> None:
    messages = inbox(2)
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(answer=True), messages=messages[:1]
    ).generate(tz_key=ZONE)
    provider = FakeAIProvider([AIAuthenticationError("bad key")])

    result = await build(session, provider, RecordingGate(answer=True), messages=messages).generate(
        tz_key=ZONE
    )

    assert result.status is BriefStatus.SAVED
    assert result.digest is not None
    assert result.digest.status is DigestStatus.PARTIAL
    assert result.error_code == "AI_AUTH_FAILED"
    assert result.coverage is not None
    assert (result.coverage.reused, result.coverage.failed) == (1, 1)
    assert provider.calls == result.ai_calls == 1


async def test_without_credentials_new_messages_fail_and_cached_ones_still_brief(
    session: AsyncSession,
) -> None:
    messages = inbox(2)
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(answer=True), messages=messages[:1]
    ).generate(tz_key=ZONE)
    provider = FakeAIProvider(credentials=False)
    gate = RecordingGate(answer=True)

    result = await build(session, provider, gate, messages=messages).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert result.digest is not None
    assert result.digest.status is DigestStatus.PARTIAL
    assert len(result.digest.items) == 1
    assert result.error_code == "AI_KEY_MISSING"
    assert result.ai_calls == provider.calls == 0
    assert gate.previews == []
    assert result.coverage is not None
    assert (result.coverage.reused, result.coverage.failed) == (1, 1)


async def test_cancel_during_planning_stops_before_the_missing_key_path(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = asyncio.Event()
    real_plan = AnalysisService.plan
    real_fail_unsent = AnalysisService.fail_unsent
    unsent_failures: list[str] = []

    async def plan_then_cancel(self: AnalysisService, **options: Any) -> AnalysisPlan:
        plan = await real_plan(self, **options)
        cancel.set()  # The owner cancels while the run is planning.
        return plan

    def record_fail_unsent(
        self: AnalysisService, plan: AnalysisPlan, error_code: str
    ) -> AnalysisRun:
        unsent_failures.append(error_code)
        return real_fail_unsent(self, plan, error_code)

    monkeypatch.setattr(AnalysisService, "plan", plan_then_cancel)
    monkeypatch.setattr(AnalysisService, "fail_unsent", record_fail_unsent)
    provider = FakeAIProvider(credentials=False)
    gate = RecordingGate(answer=True)

    result = await build(session, provider, gate).generate(tz_key=ZONE, cancel=cancel)

    assert result.status is BriefStatus.CANCELLED
    assert result.ai_calls == provider.calls == 0
    assert unsent_failures == []
    assert provider.credential_checks == 0
    assert gate.previews == []
    account_id = await account_id_of(session)
    assert await DigestRepository(session).get_by_account_and_date(account_id, TODAY) is None


async def test_credentials_are_checked_only_when_something_must_be_sent(
    session: AsyncSession,
) -> None:
    await build(session, FakeAIProvider([answer_all()]), RecordingGate(answer=True)).generate(
        tz_key=ZONE
    )
    provider = FakeAIProvider(credentials=False)

    result = await build(session, provider, RecordingGate(answer=True)).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert result.error_code is None
    assert provider.credential_checks == 0


async def test_cancel_during_analysis_keeps_results_but_writes_no_brief(
    session: AsyncSession,
) -> None:
    cancel = asyncio.Event()

    def answer_then_cancel(requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
        cancel.set()
        return answer_all()(requests)

    script: list[ScriptItem] = [answer_then_cancel]
    provider = FakeAIProvider(script)

    result = await build(session, provider, RecordingGate(answer=True), batch_size=1).generate(
        tz_key=ZONE, cancel=cancel
    )

    account_id = await account_id_of(session)
    assert result.status is BriefStatus.CANCELLED
    assert provider.calls == result.ai_calls == 1
    assert await DigestRepository(session).get_by_account_and_date(account_id, TODAY) is None
    stored = []
    for message_id in ("m0", "m1"):
        row = await MessageRepository(session).get_by_provider_message_id(account_id, message_id)
        assert row is not None
        stored.extend(await AnalysisRepository(session).get_by_message_id(row.id))
    assert len(stored) == 1


async def test_the_preview_states_the_body_limit_the_body_service_applies(
    session: AsyncSession,
) -> None:
    gate = RecordingGate(answer=True)

    await build(session, FakeAIProvider([answer_all()]), gate, body_limit=4_000).generate(
        tz_key=ZONE
    )

    (preview,) = gate.previews
    assert preview.body_character_limit == 4_000
    text = "\n".join(disclosure_lines(preview))
    assert "4,000" in text
    assert "8,000" not in text


async def test_the_disclosure_shows_the_provider_privacy_notice(session: AsyncSession) -> None:
    gate = RecordingGate(answer=True)

    await build(session, FakeAIProvider([answer_all()]), gate).generate(tz_key=ZONE)

    (preview,) = gate.previews
    assert preview.privacy_notice == "The fake provider keeps nothing."
    text = "\n".join(disclosure_lines(preview))
    assert "The fake provider keeps nothing." in text
    assert "Zero Data Retention" not in text
    assert "Groq" not in text


async def test_nothing_to_send_never_calls_the_gate(session: AsyncSession) -> None:
    gate = RecordingGate(answer=False)

    result = await build(session, FakeAIProvider(), gate, messages=[]).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert gate.previews == []
    assert result.digest is not None
    assert result.digest.status is DigestStatus.EMPTY
    assert result.coverage is not None
    assert result.coverage.shortlisted == 0


async def test_progress_reports_analyzing_then_assembling(session: AsyncSession) -> None:
    events: list[SyncProgress] = []

    await build(session, FakeAIProvider([answer_all()]), RecordingGate(answer=True)).generate(
        tz_key=ZONE, progress=events.append
    )

    stages = [event.stage for event in events]
    assert SyncStage.ANALYZING in stages
    assert stages[-1] is SyncStage.ASSEMBLING
    assert stages.index(SyncStage.ANALYZING) < stages.index(SyncStage.ASSEMBLING)
    analyzing = [event for event in events if event.stage is SyncStage.ANALYZING]
    assert [event.ai_batches_completed for event in analyzing] == [1]


def test_disclosure_lines_cover_counts_truncation_fields_and_storage() -> None:
    preview = TransmissionPreview(
        provider_name="groq",
        model_name="model-1",
        message_count=3,
        truncated_count=1,
        reused_count=2,
        first_use=True,
        body_character_limit=4_000,
        privacy_notice="The provider keeps nothing.",
    )

    text = "\n".join(disclosure_lines(preview))

    assert "send 3 messages to Groq (model-1)" in text
    assert "1 message is cut" in text
    assert all(sent_field in text for sent_field in SENT_FIELDS)
    assert "plain-text body, cut to at most 4,000 characters" in text
    assert "8,000" not in text
    assert "attachments, recipients, message IDs, links, account IDs or credentials" in text
    assert "The provider keeps nothing." in text
    assert "2 messages already analyzed" in text
    assert "remembered for this account until you revoke it" in text


def test_disclosure_lines_for_a_returning_user_without_truncation() -> None:
    preview = TransmissionPreview(
        provider_name="groq",
        model_name="model-1",
        message_count=1,
        truncated_count=0,
        reused_count=0,
        first_use=False,
        body_character_limit=8_000,
        privacy_notice="The provider keeps nothing.",
    )

    text = "\n".join(disclosure_lines(preview))

    assert "send 1 message to Groq" in text
    assert "No message is cut" in text
    assert "remembered" not in text
    assert "already analyzed" not in text


async def test_body_text_outside_the_evidence_never_reaches_disk_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "leak.sqlite3"
    database = Database.from_path(path)
    await database.create_schema_for_tests()
    messages = inbox()
    texts = {
        message.provider_message_id: f"{BODY} {MARKER} private detail." for message in messages
    }
    try:
        async with database.session() as session:
            result = await build(
                session,
                FakeAIProvider([answer_all()]),
                RecordingGate(answer=True),
                messages=messages,
                texts=texts,
            ).generate(tz_key=ZONE)
    finally:
        await database.dispose()

    assert result.status is BriefStatus.SAVED
    assert all(MARKER not in text[:40] for text in texts.values())  # Outside the evidence.
    stored = database_bytes(tmp_path, "leak.sqlite3")
    assert b"quarterly budget" in stored  # The evidence excerpt is stored...
    assert MARKER.encode() not in stored  # ...but nothing else from the body.
    assert MARKER not in caplog.text


async def test_a_body_returned_whole_as_evidence_is_never_stored_whole(tmp_path: Path) -> None:
    path = tmp_path / "whole.sqlite3"
    database = Database.from_path(path)
    await database.create_schema_for_tests()
    messages = inbox(1)
    body = (
        "Hi team, please approve the revised quarterly budget before Friday so finance can "
        "close the books. The new numbers cover travel, the office move and two contractor "
        "renewals. Reply here if anything looks wrong and I will update the sheet today."
    )
    assert 240 <= len(body) <= 260
    try:
        async with database.session() as session:
            result = await build(
                session,
                FakeAIProvider(
                    [lambda requests: answer_all(evidence=requests[0].body_text)(requests)]
                ),
                RecordingGate(answer=True),
                messages=messages,
                texts={messages[0].provider_message_id: body},
            ).generate(tz_key=ZONE)
    finally:
        await database.dispose()

    assert result.status is BriefStatus.SAVED
    assert result.digest is not None
    (item,) = result.digest.items
    assert item.evidence is not None
    assert item.evidence.endswith("…")
    stored = database_bytes(tmp_path, "whole.sqlite3")
    assert body[:100].encode() in stored  # A cut excerpt is stored...
    assert body.encode() not in stored  # ...but never the whole body.


@dataclass
class ReviewGate:
    selected: tuple[str, ...] | None

    async def review(
        self,
        candidates: tuple[RankedMessage, ...],
        selected_ids: tuple[str, ...],
        *,
        blocked_ids: frozenset[str],
        outside_ids: frozenset[str],
        declined_ids: frozenset[str],
        limit: int,
    ) -> tuple[str, ...] | None:
        return self.selected


async def test_review_can_replace_suggestion_with_other_inbox_message(
    session: AsyncSession,
) -> None:
    mailbox = inbox(12)
    provider = FakeAIProvider([answer_all()])
    service = build(session, provider, RecordingGate(True), messages=mailbox)

    class IncludeOther:
        async def review(
            self,
            candidates: tuple[RankedMessage, ...],
            selected_ids: tuple[str, ...],
            *,
            blocked_ids: frozenset[str],
            outside_ids: frozenset[str],
            declined_ids: frozenset[str],
            limit: int,
        ) -> tuple[str, ...]:
            assert len(candidates) == 12
            assert len(selected_ids) == 10
            omitted = next(
                item.message.provider_message_id
                for item in candidates
                if item.message.provider_message_id not in selected_ids
            )
            return (omitted,)

    result = await service.generate(tz_key=ZONE, shortlist_gate=IncludeOther())
    assert result.digest is not None
    assert len(result.digest.items) == 1
    assert result.digest.items[0].message_key in {"m0", "m1"}
    assert provider.calls == 1


async def test_oversized_review_is_rejected_before_bodies(session: AsyncSession) -> None:
    provider = FakeAIProvider()
    service = build(session, provider, RecordingGate(True), messages=inbox(12))
    with pytest.raises(ValueError, match="up to ten"):
        await service.generate(
            tz_key=ZONE, shortlist_gate=ReviewGate(tuple(f"m{index}" for index in range(11)))
        )
    assert provider.calls == 0


async def test_review_filters_before_body_retrieval(session: AsyncSession) -> None:
    provider = FakeAIProvider([answer_all()])
    service = build(session, provider, RecordingGate(True))
    fetched: list[str] = []

    class TrackingReader(FakeBodyReader):
        async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
            fetched.append(provider_message_id)
            return await super().fetch_message_body(provider_message_id)

    reader = TrackingReader(texts_for(inbox()))
    service._bodies = BodyService(reader)
    result = await service.generate(tz_key=ZONE, shortlist_gate=ReviewGate(("m1",)))
    assert fetched == ["m1"]
    assert result.sync.shortlisted_message_keys == ("m1",)
    assert result.coverage is not None and result.coverage.shortlisted == 1
    assert provider.calls == 1


@pytest.mark.parametrize("selection", [None, (), ("foreign",), ("m0", "m0")])
async def test_review_cancel_empty_and_invalid_never_fetch_bodies(
    session: AsyncSession,
    selection: tuple[str, ...] | None,
) -> None:
    provider = FakeAIProvider()
    service = build(session, provider, RecordingGate(True))

    class NoBodies:
        async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
            pytest.fail("No body should be fetched")

    service._bodies = BodyService(NoBodies())
    if selection in (("foreign",), ("m0", "m0")):
        with pytest.raises(ValueError, match="unique members"):
            await service.generate(tz_key=ZONE, shortlist_gate=ReviewGate(selection))
    else:
        result = await service.generate(tz_key=ZONE, shortlist_gate=ReviewGate(selection))
        assert result.status is (BriefStatus.CANCELLED if selection is None else BriefStatus.SAVED)
    assert provider.calls == 0


BLOCKED_SENDER = "news@lists.example.org"
RULES = ("@example.org",)


def mixed_inbox() -> list[NormalizedMessage]:
    """m0 and m2 come from a sender the rules exclude; m1 and m3 don't."""
    return [
        make_message(
            provider_message_id=f"m{index}",
            subject=f"Budget {index}",
            sender=EmailContact(address=BLOCKED_SENDER if index % 2 == 0 else "boss@example.com"),
            received_at_utc=datetime(2026, 9, 4, 13, index, tzinfo=UTC),
            web_link=f"https://mail.example.com/m{index}",
        )
        for index in range(4)
    ]


class RecordingReader(FakeBodyReader):
    def __init__(self, texts: dict[str, str]) -> None:
        super().__init__(texts)
        self.fetched: list[str] = []

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        self.fetched.append(provider_message_id)
        return await super().fetch_message_body(provider_message_id)


def with_reader(service: BriefService, mailbox: Sequence[NormalizedMessage]) -> RecordingReader:
    reader = RecordingReader(texts_for(mailbox))
    service._bodies = BodyService(reader)
    return reader


async def test_excluded_senders_are_never_selected_read_or_sent(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    mailbox = mixed_inbox()
    provider = FakeAIProvider([answer_all()])
    service = build(session, provider, RecordingGate(True), messages=mailbox)
    reader = with_reader(service, mailbox)

    with caplog.at_level(logging.DEBUG, logger="mailbrief"):
        result = await service.generate(tz_key=ZONE, excluded_senders=RULES)

    assert result.sync.shortlisted_message_keys == ("m3", "m1")
    assert sorted(reader.fetched) == ["m1", "m3"]
    (batch,) = provider.batches
    assert {request.sender.address for request in batch} == {"boss@example.com"}
    assert {request.subject for request in batch} == {"Budget 1", "Budget 3"}
    assert "2 messages" in caplog.text and "example.org" not in caplog.text


class OfferedGate:
    """Records what review offered, then answers with a fixed selection."""

    def __init__(self, selected: tuple[str, ...]) -> None:
        self.selected = selected
        self.offered: tuple[tuple[str, ...], frozenset[str], int] | None = None

    async def review(
        self,
        candidates: tuple[RankedMessage, ...],
        selected_ids: tuple[str, ...],
        *,
        blocked_ids: frozenset[str],
        outside_ids: frozenset[str],
        declined_ids: frozenset[str],
        limit: int,
    ) -> tuple[str, ...] | None:
        self.offered = (
            tuple(item.message.provider_message_id for item in candidates),
            blocked_ids,
            limit,
        )
        assert not blocked_ids & set(selected_ids)
        return self.selected


async def test_review_sees_blocked_messages_but_can_t_select_them(
    session: AsyncSession,
) -> None:
    mailbox = mixed_inbox()
    provider = FakeAIProvider()
    service = build(session, provider, RecordingGate(True), messages=mailbox)
    reader = with_reader(service, mailbox)
    gate = OfferedGate(("m1", "m2"))

    with pytest.raises(ExcludedSenderError):
        await service.generate(
            tz_key=ZONE, shortlist_gate=gate, excluded_senders=RULES, shortlist_limit=4
        )

    assert gate.offered is not None
    assert sorted(gate.offered[0]) == ["m0", "m1", "m2", "m3"]
    assert gate.offered[1:] == (frozenset({"m0", "m2"}), 4)
    assert reader.fetched == []
    assert provider.calls == 0


async def test_an_excluded_include_fails_before_bodies_and_ai(session: AsyncSession) -> None:
    mailbox = mixed_inbox()
    provider = FakeAIProvider()
    gate = RecordingGate(True)
    service = build(session, provider, gate, messages=mailbox)
    reader = with_reader(service, mailbox)

    with pytest.raises(ExcludedSenderError):
        await service.generate(tz_key=ZONE, include_ids=("m0",), excluded_senders=RULES)

    assert reader.fetched == []
    assert provider.calls == 0
    assert gate.previews == []


async def test_the_limit_caps_automatic_and_reviewed_selection(session: AsyncSession) -> None:
    mailbox = inbox(5)
    provider = FakeAIProvider([answer_all()])
    service = build(session, provider, RecordingGate(True), messages=mailbox)
    reader = with_reader(service, mailbox)

    result = await service.generate(tz_key=ZONE, shortlist_limit=2)

    assert len(result.sync.shortlisted_message_keys) == 2
    assert len(reader.fetched) == 2

    oversized = OfferedGate(("m0", "m1", "m2"))
    with pytest.raises(ShortlistReviewError, match="up to two"):
        await service.generate(tz_key=ZONE, shortlist_gate=oversized, shortlist_limit=2)
    assert oversized.offered is not None and oversized.offered[2] == 2
    assert len(reader.fetched) == 2


YESTERDAY = date(2026, 9, 3)


class RangeProvider(FakeEmailProvider):
    """Serves only the messages in the requested range and records every range and connect."""

    def __init__(self, messages: Sequence[NormalizedMessage]) -> None:
        super().__init__(pages=[list(messages)])
        self.messages = list(messages)
        self.ranges: list[tuple[datetime, datetime]] = []
        self.connects = 0

    async def connect(self) -> AccountIdentity:
        self.connects += 1
        return await super().connect()

    async def iter_message_pages(
        self,
        *,
        range_start_utc: datetime,
        range_end_utc: datetime,
        continuation: str | None = None,
    ) -> AsyncIterator[MessagePage]:
        self.ranges.append((range_start_utc, range_end_utc))
        chosen = tuple(
            message
            for message in self.messages
            if range_start_utc <= message.received_at_utc < range_end_utc
        )
        yield MessagePage(page_number=1, messages=chosen, continuation=None)


def two_days() -> list[NormalizedMessage]:
    """y0 and y1 arrived yesterday in Toronto, t0 today."""
    return [
        make_message(
            provider_message_id=key,
            subject=f"Budget {key}",
            received_at_utc=received,
            web_link=f"https://mail.example.com/{key}",
        )
        for key, received in (
            ("y0", datetime(2026, 9, 3, 14, tzinfo=UTC)),
            ("y1", datetime(2026, 9, 4, 3, 30, tzinfo=UTC)),  # 23:30 on the 3rd in Toronto.
            ("t0", datetime(2026, 9, 4, 13, tzinfo=UTC)),
        )
    ]


async def last_sync(session: AsyncSession) -> datetime | None:
    account = await AccountRepository(session).get_by_email("user@example.com")
    assert account is not None
    await session.refresh(account)
    return account.last_sync_at_utc


async def test_a_past_day_syncs_exactly_its_window_and_saves_under_its_date(
    session: AsyncSession,
) -> None:
    provider = RangeProvider(two_days())
    ai = FakeAIProvider([answer_all(), answer_all()])
    service = build(session, ai, RecordingGate(True), email=provider, texts=texts_for(two_days()))

    today = await service.generate(tz_key=ZONE)
    synced_today = await last_sync(session)
    past = await service.generate(tz_key=ZONE, local_date=YESTERDAY)

    assert provider.ranges[1] == (
        datetime(2026, 9, 3, 4, tzinfo=UTC),  # Midnight in Toronto.
        datetime(2026, 9, 4, 4, tzinfo=UTC),
    )
    assert past.digest is not None and past.digest.local_date == YESTERDAY
    assert {item.message_key for item in past.digest.items} == {"y0", "y1"}
    assert today.digest is not None and today.digest.local_date == TODAY
    assert synced_today is not None
    assert await last_sync(session) == synced_today  # Only today's window moves it.
    account = await AccountRepository(session).get_by_email("user@example.com")
    assert account is not None
    kept = await DigestRepository(session).get_by_account_and_date(account.id, TODAY)
    assert kept is not None  # Today's brief is untouched.


async def test_a_past_day_reconciles_its_inbox_membership(session: AsyncSession) -> None:
    """A message archived since it was cached leaves a past day's brief."""
    provider = RangeProvider(two_days())
    ai = FakeAIProvider([answer_all()])  # The second run reuses y0's analysis.
    service = build(session, ai, RecordingGate(True), email=provider, texts=texts_for(two_days()))
    await service.generate(tz_key=ZONE, local_date=YESTERDAY)
    provider.messages = [m for m in provider.messages if m.provider_message_id != "y1"]

    again = await service.generate(tz_key=ZONE, local_date=YESTERDAY)

    assert again.digest is not None
    assert [item.message_key for item in again.digest.items] == ["y0"]
    account = await AccountRepository(session).get_by_email("user@example.com")
    assert account is not None
    rows = await MessageRepository(session).get_messages_in_range(
        account.id,
        datetime(2026, 9, 3, 4, tzinfo=UTC),
        datetime(2026, 9, 4, 4, tzinfo=UTC),
        inbox_only=False,
    )
    assert {row.provider_message_id: row.is_in_inbox for row in rows} == {
        "y0": True,
        "y1": False,
    }


@pytest.mark.parametrize("day", [date(2026, 8, 27), date(2026, 9, 5)])
async def test_an_out_of_range_date_fails_before_gmail_or_the_database(
    session: AsyncSession, day: date
) -> None:
    provider = RangeProvider(two_days())
    ai = FakeAIProvider()
    service = build(session, ai, RecordingGate(True), email=provider)

    with pytest.raises(BriefDateError):
        await service.generate(tz_key=ZONE, local_date=day)

    assert provider.connects == 0 and provider.ranges == []
    assert await session.scalar(select(func.count()).select_from(SyncRunTable)) == 0
    assert await session.scalar(select(func.count()).select_from(AccountTable)) == 0
    assert await DigestRepository(session).get_latest() is None


async def test_seven_days_back_is_allowed(session: AsyncSession) -> None:
    provider = RangeProvider(two_days())
    service = build(session, FakeAIProvider(), RecordingGate(True), email=provider)

    result = await service.generate(tz_key=ZONE, local_date=date(2026, 8, 28))

    assert result.digest is not None and result.digest.local_date == date(2026, 8, 28)
    assert result.digest.status is DigestStatus.EMPTY


async def link_m0_to_an_action(session: AsyncSession, key: str = "m0") -> str:
    """An open action whose source is the cached message ``key``; a later message in its
    thread follows it."""
    account = await AccountRepository(session).get_by_email("user@example.com")
    assert account is not None
    message = await MessageRepository(session).get_by_provider_message_id(account.id, key)
    assert message is not None
    repository = ActionRepository(session)
    row = await repository.add_action(
        ActionTable(
            public_id="00000000-0000-4000-8000-000000000001",
            title="Approve the budget",
            ownership="mine",
            status="open",
            deadline_precision="none",
            created_at_utc=NOW,
            updated_at_utc=NOW,
            revision=1,
        )
    )
    await repository.add_source(row.id, message, account)
    await session.commit()
    return row.public_id


async def test_a_saved_brief_turns_follow_up_signals_into_proposals(
    session: AsyncSession,
) -> None:
    signal = answer_all(follow_up="cancelled", follow_up_evidence="waiting on it")
    first = await build(session, FakeAIProvider([signal]), RecordingGate(answer=True)).generate(
        tz_key=ZONE
    )
    public_id = await link_m0_to_an_action(session)

    # Again, from the cached analyses: m1 continues the action's thread; m0 is its source.
    second = await build(session, FakeAIProvider(), RecordingGate(answer=True)).generate(
        tz_key=ZONE
    )

    assert (first.proposals_created, second.proposals_created) == (0, 1)
    assert second.status is BriefStatus.SAVED
    (proposal,) = await ProposalService(session).pending()
    assert (proposal.action_public_id, proposal.provider_message_id) == (public_id, "m1")


async def test_a_past_day_s_brief_makes_proposals_too(session: AsyncSession) -> None:
    provider = RangeProvider(two_days())
    signal = answer_all(follow_up="cancelled", follow_up_evidence="waiting on it")
    texts = texts_for(two_days())
    first = await build(
        session, FakeAIProvider([signal]), RecordingGate(True), email=provider, texts=texts
    ).generate(tz_key=ZONE, local_date=YESTERDAY)
    public_id = await link_m0_to_an_action(session, "y0")

    # Again, from the cached analyses: y1 follows y0, the action's source, in its thread.
    second = await build(
        session, FakeAIProvider(), RecordingGate(True), email=provider, texts=texts
    ).generate(tz_key=ZONE, local_date=YESTERDAY)

    assert (first.proposals_created, second.proposals_created) == (0, 1)
    (proposal,) = await ProposalService(session).pending()
    assert (proposal.action_public_id, proposal.provider_message_id) == (public_id, "y1")


async def seed_outside_reply(session: AsyncSession) -> None:
    """An open action tracking thread "deck", and its archived reply from two hours ago."""
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id="acc-1",
            email_address="user@example.com",
        )
    )
    since = NOW - timedelta(days=3)
    row = await ActionRepository(session).add_action(
        ActionTable(
            public_id="00000000-0000-4000-8000-000000000002",
            title="Send the deck",
            ownership="mine",
            status="open",
            deadline_precision="none",
            created_at_utc=since,
            updated_at_utc=since,
            revision=1,
        )
    )
    session.add(
        ActionSourceTable(
            action_id=row.id,
            provider_message_id="source-deck",
            subject="Deck",
            sender_address="alex@example.com",
            web_link="https://mail.google.com/mail/u/#all/x",
            received_at_utc=since,
            provider=account.provider,
            provider_account_id=account.provider_account_id,
            provider_thread_id="deck",
        )
    )
    archived = make_message(
        provider_message_id="archived",
        conversation_id="deck",
        subject="Re: Deck",
        received_at_utc=NOW - timedelta(hours=2),
        is_in_inbox=False,
        web_link="https://mail.example.com/archived",
    )
    await MessageRepository(session).upsert_messages(account.id, [archived])
    await session.commit()


async def test_an_outside_reply_becomes_a_follow_up_item_and_the_coverage_line_says_so(
    session: AsyncSession,
) -> None:
    await seed_outside_reply(session)
    mailbox = inbox()
    texts = {**texts_for(mailbox), "archived": f"{BODY} Reference archived."}
    service = build(
        session,
        FakeAIProvider([answer_all()]),
        RecordingGate(True),
        messages=mailbox,
        texts=texts,
        threads=RecordingThreads(session, ThreadCheck()),
    )

    result = await service.generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert result.sync.outside_ids == {"archived"}
    assert result.coverage is not None and result.coverage.shortlisted == 3
    digest = result.digest
    assert digest is not None
    # Whatever its category, it is in its own section, last.
    assert [(item.message_key, item.section) for item in digest.items][-1] == (
        "archived",
        DigestSection.FOLLOW_UPS,
    )
    assert DigestSection.FOLLOW_UPS not in {item.section for item in digest.items[:-1]}
    assert coverage_line(digest).endswith(
        " Also includes 1 reply from a thread you track that wasn't in today's Inbox."
    )
    # Saved that way.
    account = await AccountRepository(session).get_by_email("user@example.com")
    assert account is not None
    repository = DigestRepository(session)
    row = await repository.get_by_account_and_date(account.id, TODAY)
    assert row is not None
    stored = await repository.get_digest_items(row.id)
    assert [item.section for item, _, _ in stored][-1] == DigestSection.FOLLOW_UPS.value


async def test_an_outside_reply_goes_through_the_same_consent_and_limit(
    session: AsyncSession,
) -> None:
    await seed_outside_reply(session)
    mailbox = inbox(2)
    gate = RecordingGate(False)
    service = build(
        session,
        FakeAIProvider(),
        gate,
        messages=mailbox,
        texts={**texts_for(mailbox), "archived": f"{BODY} Reference archived."},
        threads=RecordingThreads(session, ThreadCheck()),
    )

    declined = await service.generate(tz_key=ZONE)

    # Declining sends nothing, the outside reply included, and saves no brief.
    assert declined.status is BriefStatus.CONSENT_DECLINED
    (preview,) = gate.previews
    assert preview.message_count == 3  # The two Inbox messages and the outside reply.
    assert await DigestRepository(session).get_latest() is None

    limited = build(
        session,
        FakeAIProvider([answer_all()]),
        RecordingGate(True),
        messages=mailbox,
        texts={**texts_for(mailbox), "archived": f"{BODY} Reference archived."},
        threads=RecordingThreads(session, ThreadCheck()),
    )
    result = await limited.generate(tz_key=ZONE, shortlist_limit=1)
    assert result.coverage is not None and result.coverage.shortlisted == 1
    assert result.sync.outside_ids == {"archived"}  # The tracked bonus wins the one place.


class FailingProposals(ProposalService):
    async def derive(self, *_args: object, **_kwargs: object) -> int:
        raise RuntimeError("PRIVATE-DETAIL")


async def test_a_failure_to_propose_never_fails_the_saved_brief(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    provider = FakeAIProvider([answer_all()])
    caplog.set_level(logging.WARNING)

    result = await build(
        session, provider, RecordingGate(answer=True), proposals=FailingProposals(session)
    ).generate(tz_key=ZONE)

    assert result.status is BriefStatus.SAVED
    assert result.proposals_created == 0
    assert "Follow-up proposals failed: RuntimeError" in caplog.text
    assert "PRIVATE-DETAIL" not in caplog.text
