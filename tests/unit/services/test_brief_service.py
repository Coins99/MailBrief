"""Brief service: consent before sending, caching, failures, cancellation and leak safety."""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import AIUsage, AnalysisRequest, AnalysisResponse
from mailbrief.domain.bodies import BodySource, MessageBody
from mailbrief.domain.briefs import SENT_FIELDS, BriefStatus, TransmissionPreview
from mailbrief.domain.digests import DigestStatus, SyncProgress, SyncStage
from mailbrief.domain.messages import NormalizedMessage
from mailbrief.ports.errors import AIAuthenticationError, ProviderPermissionError
from mailbrief.services.analysis import AnalysisService
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import CONSENT_DISCLOSURE_VERSION, BriefService, disclosure_lines
from mailbrief.services.digest import DigestService
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    AnalysisRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
    SyncRunRepository,
)
from tests.factories import make_message
from tests.unit.services.ai_fakes import FakeAIProvider, ScriptItem, answer_all
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
) -> BriefService:
    mailbox = list(inbox() if messages is None else messages)
    email = email or FakeEmailProvider(pages=[mailbox])
    application = ApplicationService(
        provider=email,
        message_repo=MessageRepository(session),
        sync_run_repo=SyncRunRepository(session),
        account_repo=AccountRepository(session),
    )
    gate.provider = provider
    return BriefService(
        session=session,
        application=application,
        bodies=BodyService(FakeBodyReader(texts_for(mailbox) if texts is None else texts)),
        analysis=AnalysisService(session, provider, batch_size=batch_size),
        digests=DigestService(session),
        consent_gate=gate,
        clock=lambda: NOW,
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
    )

    text = "\n".join(disclosure_lines(preview))

    assert "send 3 messages to Groq (model-1)" in text
    assert "1 message is cut" in text
    assert all(sent_field in text for sent_field in SENT_FIELDS)
    assert "attachments, recipients, message IDs, links, account IDs or credentials" in text
    assert "Zero Data Retention" in text
    assert "cannot verify" in text
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
