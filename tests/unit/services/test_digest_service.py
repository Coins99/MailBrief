"""Digest service: sections, deterministic order, status rules and coverage."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import AnalysisCategory, DeadlinePrecision, MessageAnalysis
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.digests import DigestCoverage, DigestSection, DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind, RankedMessage
from mailbrief.services.analysis import PlannedMessage
from mailbrief.services.calendar import DayWindow
from mailbrief.services.digest import DigestService, section_for
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    AnalysisRepository,
    DigestRepository,
    MessageRepository,
)
from tests.factories import make_analysis, make_message

OWNER = "owner@example.com"
WINDOW = DayWindow(
    local_date=date(2026, 9, 4),
    timezone_name="America/Toronto",
    start_utc=datetime(2026, 9, 4, 4, 0, tzinfo=UTC),
    end_utc=datetime(2026, 9, 5, 4, 0, tzinfo=UTC),
)
NOON = datetime(2026, 9, 4, 16, 0, tzinfo=UTC)
NO_DEADLINE: dict[str, object] = {
    "deadline_text": None,
    "deadline_precision": DeadlinePrecision.NONE,
    "deadline_date": None,
    "deadline_at_utc": None,
    "deadline_timezone": None,
}
NO_ACTION: dict[str, object] = {"action_required": False, "action_text": None}


def action(**overrides: object) -> MessageAnalysis:
    return make_analysis(**overrides)


def deadline(**overrides: object) -> MessageAnalysis:
    return make_analysis(category=AnalysisCategory.DEADLINE, **NO_ACTION, **overrides)


def decision() -> MessageAnalysis:
    return make_analysis(category=AnalysisCategory.DECISION, **NO_ACTION, **NO_DEADLINE)


def highlight() -> MessageAnalysis:
    return make_analysis(category=AnalysisCategory.INFORMATION, **NO_ACTION, **NO_DEADLINE)


DATE_ONLY: dict[str, object] = {
    **NO_DEADLINE,
    "deadline_text": "Friday",
    "deadline_precision": DeadlinePrecision.DATE,
    "deadline_date": date(2026, 9, 4),
    "deadline_timezone": "America/Toronto",
}
UNRESOLVED: dict[str, object] = {
    **NO_DEADLINE,
    "deadline_text": "ASAP",
    "deadline_precision": DeadlinePrecision.UNRESOLVED,
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


@pytest.fixture
async def account_id(session: AsyncSession) -> int:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.MICROSOFT, provider_account_id="acc-1", email_address=OWNER
        )
    )
    await session.commit()
    return account.id


async def entry(
    session: AsyncSession,
    account_id: int,
    key: str,
    analysis: MessageAnalysis | None,
    *,
    score: int = 10,
    received: datetime = NOON,
    outcome: AnalysisOutcome = AnalysisOutcome.ANALYZED,
) -> PlannedMessage:
    message = make_message(
        provider_message_id=key,
        subject=f"Subject {key}",
        received_at_utc=received,
        web_link=f"https://mail.example.com/{key}",
    )
    (row,) = await MessageRepository(session).upsert_messages(account_id, [message])
    ranked = RankedMessage(message=message, score=score)
    if analysis is None:
        return PlannedMessage(ranked, row.id, None, None, outcome)
    stored = await AnalysisRepository(session).upsert_analysis(
        message_id=row.id,
        input_hash=f"hash-{key}",
        provider="fake",
        model="fake-model",
        prompt_version="fake-1",
        schema_version="2",
        analysis=analysis,
    )
    return PlannedMessage(ranked, row.id, None, f"hash-{key}", outcome, analysis, stored.id)


def coverage(
    *, analyzed: int = 0, reused: int = 0, failed: int = 0, skipped: int = 0, complete: bool = True
) -> DigestCoverage:
    return DigestCoverage(
        sync_complete=complete,
        shortlisted=analyzed + reused + failed + skipped,
        analyzed=analyzed,
        reused=reused,
        failed=failed,
        skipped=skipped,
        input_tokens=120 if analyzed else None,
        output_tokens=30 if analyzed else None,
        ai_provider="fake" if analyzed or reused else None,
        ai_model="fake-model" if analyzed or reused else None,
    )


@pytest.mark.parametrize(
    ("analysis", "section"),
    [
        (action(category=AnalysisCategory.DECISION), DigestSection.ACTIONS),
        (deadline(**UNRESOLVED), DigestSection.DEADLINES),
        (
            make_analysis(category=AnalysisCategory.DECISION, **NO_ACTION, **DATE_ONLY),
            DigestSection.DEADLINES,
        ),
        (decision(), DigestSection.DECISIONS),
        (highlight(), DigestSection.HIGHLIGHTS),
    ],
    ids=["action-wins", "unresolved-deadline", "deadline-beats-decision", "decision", "highlight"],
)
def test_section_precedence(analysis: MessageAnalysis, section: DigestSection) -> None:
    assert section_for(analysis) is section


async def test_items_follow_sections_due_times_then_tie_breaks(
    session: AsyncSession, account_id: int
) -> None:
    earlier = datetime(2026, 9, 4, 13, 0, tzinfo=UTC)
    later = datetime(2026, 9, 4, 17, 0, tzinfo=UTC)
    planned = [
        await entry(session, account_id, "h-low", highlight(), score=5, received=earlier),
        await entry(session, account_id, "decide", decision()),
        await entry(session, account_id, "d-unresolved", deadline(**UNRESOLVED), score=50),
        await entry(session, account_id, "a-none", action(**NO_DEADLINE), score=50),
        await entry(session, account_id, "h-high", highlight(), score=9),
        await entry(session, account_id, "d-date", deadline(**DATE_ONLY), score=50),
        await entry(session, account_id, "a-time", action(), score=1),
        await entry(session, account_id, "h-new", highlight(), score=5, received=later),
        await entry(session, account_id, "d-time", deadline(), score=1),
        await entry(session, account_id, "h-a", highlight(), score=5, received=earlier),
        await entry(session, account_id, "skipped", None, outcome=AnalysisOutcome.SKIPPED),
    ]

    digest = await DigestService(session).save(
        account_id=account_id,
        account_email=OWNER,
        window=WINDOW,
        messages=planned,
        coverage=coverage(analyzed=10, skipped=1),
    )

    assert digest is not None
    assert [item.message_key for item in digest.items] == [
        "a-time",
        "a-none",
        "d-time",
        "d-date",
        "d-unresolved",
        "decide",
        "h-high",
        "h-new",
        "h-a",
        "h-low",
    ]
    assert [item.position for item in digest.items] == list(range(10))
    assert digest.status is DigestStatus.COMPLETE
    assert digest.account_id == OWNER


@pytest.mark.parametrize(
    ("outcomes", "complete", "expected"),
    [
        ([AnalysisOutcome.ANALYZED, AnalysisOutcome.REUSED], True, DigestStatus.COMPLETE),
        ([AnalysisOutcome.ANALYZED, AnalysisOutcome.FAILED], True, DigestStatus.PARTIAL),
        ([AnalysisOutcome.REUSED], False, DigestStatus.PARTIAL),
        ([AnalysisOutcome.SKIPPED], True, DigestStatus.EMPTY),
        ([], True, DigestStatus.EMPTY),
    ],
    ids=["complete", "partial-failed", "partial-sync", "empty-skipped", "empty-shortlist"],
)
async def test_status_follows_results_failures_and_sync(
    session: AsyncSession,
    account_id: int,
    outcomes: list[AnalysisOutcome],
    complete: bool,
    expected: DigestStatus,
) -> None:
    planned = []
    for index, outcome in enumerate(outcomes):
        analysis = (
            highlight() if outcome in (AnalysisOutcome.ANALYZED, AnalysisOutcome.REUSED) else None
        )
        planned.append(await entry(session, account_id, f"m-{index}", analysis, outcome=outcome))
    counts = {outcome: outcomes.count(outcome) for outcome in AnalysisOutcome}

    digest = await DigestService(session).save(
        account_id=account_id,
        account_email=OWNER,
        window=WINDOW,
        messages=planned,
        coverage=coverage(
            analyzed=counts[AnalysisOutcome.ANALYZED],
            reused=counts[AnalysisOutcome.REUSED],
            failed=counts[AnalysisOutcome.FAILED],
            skipped=counts[AnalysisOutcome.SKIPPED],
            complete=complete,
        ),
    )

    assert digest is not None
    assert digest.status is expected


async def test_total_failure_returns_none_and_keeps_the_saved_brief(
    session: AsyncSession, account_id: int
) -> None:
    service = DigestService(session)
    good = [await entry(session, account_id, "good", highlight())]
    saved = await service.save(
        account_id=account_id,
        account_email=OWNER,
        window=WINDOW,
        messages=good,
        coverage=coverage(analyzed=1),
    )
    failed = [await entry(session, account_id, "bad", None, outcome=AnalysisOutcome.FAILED)]

    result = await service.save(
        account_id=account_id,
        account_email=OWNER,
        window=WINDOW,
        messages=failed,
        coverage=coverage(failed=1),
    )

    repo = DigestRepository(session)
    row = await repo.get_by_account_and_date(account_id, WINDOW.local_date)
    assert result is None
    assert row is not None
    kept = DigestRepository.to_domain(row, await repo.get_digest_items(row.id), OWNER)
    assert kept == saved


async def test_coverage_round_trips_and_a_resave_replaces_it(
    session: AsyncSession, account_id: int
) -> None:
    service = DigestService(session)
    first_coverage = coverage(analyzed=1)
    planned = [await entry(session, account_id, "only", highlight())]
    first = await service.save(
        account_id=account_id,
        account_email=OWNER,
        window=WINDOW,
        messages=planned,
        coverage=first_coverage,
    )
    planned[0].outcome = AnalysisOutcome.REUSED
    second_coverage = coverage(reused=1, complete=False)
    # A caller (such as a UI showing the brief) still holds the loaded row during the re-save.
    held = await DigestRepository(session).get_by_account_and_date(account_id, WINDOW.local_date)

    second = await service.save(
        account_id=account_id,
        account_email=OWNER,
        window=WINDOW,
        messages=planned,
        coverage=second_coverage,
    )

    assert held is not None
    assert held.reused_count == 1
    assert first is not None and second is not None
    assert first.coverage == first_coverage
    assert second.coverage == second_coverage
    assert second.status is DigestStatus.PARTIAL
    assert [item.evidence for item in second.items] == [highlight().evidence]


async def test_an_included_message_without_rows_is_rejected(
    session: AsyncSession, account_id: int
) -> None:
    broken = await entry(session, account_id, "broken", None, outcome=AnalysisOutcome.ANALYZED)

    with pytest.raises(ValueError, match="needs its analysis"):
        await DigestService(session).save(
            account_id=account_id,
            account_email=OWNER,
            window=WINDOW,
            messages=[broken],
            coverage=coverage(analyzed=1),
        )
