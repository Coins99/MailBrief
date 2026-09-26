"""Analysis service: planning, caching, batching, retries, errors and minimized requests."""

import asyncio
import itertools
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.config import Settings
from mailbrief.domain.analysis import (
    AIUsage,
    AnalysisProblem,
    AnalysisRequest,
    AnalysisResponse,
    DeadlinePrecision,
)
from mailbrief.domain.bodies import BodySource, BodyStatus, PreparedBody
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.digests import SyncProgress, SyncStage
from mailbrief.domain.messages import AccountIdentity, EmailContact, ProviderKind, RankedMessage
from mailbrief.ports.ai_provider import AIProvider
from mailbrief.ports.errors import (
    AIAuthenticationError,
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from mailbrief.providers.groq.credentials import ENTRY, SERVICE, GroqKeyStore
from mailbrief.providers.groq.factory import groq_provider
from mailbrief.services.analysis import (
    AnalysisRun,
    AnalysisService,
    PlannedMessage,
    input_hash,
    validate_candidate,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, AnalysisRepository, MessageRepository
from mailbrief.storage.tables import AnalysisTable
from tests.factories import make_message
from tests.unit.providers.groq.groq_fixtures import (
    CHAT_URL,
    TEST_KEY,
    MemoryVault,
    answer_every_message,
    error_body,
    sent_messages,
)
from tests.unit.services.ai_fakes import FakeAIProvider, ScriptItem, answer_all, good_candidate

ZONE = "America/Toronto"
OWNER = "owner@example.com"
BODY = "Please approve the quarterly budget by Friday 5 PM. The finance team needs it."
ANALYZED = AnalysisOutcome.ANALYZED
FAILED = AnalysisOutcome.FAILED


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


async def seed(session: AsyncSession, count: int) -> tuple[int, list[RankedMessage]]:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.MICROSOFT, provider_account_id="acc-1", email_address=OWNER
        )
    )
    messages = [
        make_message(
            provider_message_id=f"msg-{index}",
            subject=f"Budget item {index}",
            received_at_utc=datetime(2026, 9, 4, 12, index, tzinfo=UTC),
            web_link=f"https://mail.example.com/msg-{index}",
        )
        for index in range(count)
    ]
    if messages:
        await MessageRepository(session).upsert_messages(account.id, messages)
    await session.commit()
    ranked = [
        RankedMessage(message=message, score=30 - index) for index, message in enumerate(messages)
    ]
    return account.id, ranked


def ready(shortlist: Sequence[RankedMessage], *, truncated: bool = False) -> list[PreparedBody]:
    return [
        PreparedBody(
            provider_message_id=item.message.provider_message_id,
            status=BodyStatus.READY,
            text=f"{BODY} Reference {index}.",
            source=BodySource.PLAIN,
            truncated=truncated,
        )
        for index, item in enumerate(shortlist)
    ]


def counting_keys() -> Callable[[], str]:
    numbers = itertools.count(1)
    return lambda: f"{next(numbers):08x}"


async def analyze(
    session: AsyncSession,
    provider: AIProvider,
    shortlist: Sequence[RankedMessage],
    *,
    account_id: int,
    batch_size: int = 5,
    cancel: asyncio.Event | None = None,
    progress: Callable[[SyncProgress], None] | None = None,
) -> AnalysisRun:
    service = AnalysisService(session, provider, batch_size=batch_size, key_factory=counting_keys())
    plan = await service.plan(
        account_id=account_id, shortlist=shortlist, bodies=ready(shortlist), timezone_name=ZONE
    )
    return await service.execute(plan, cancel=cancel, progress=progress)


def outcomes(run: AnalysisRun) -> list[AnalysisOutcome | None]:
    return [item.outcome for item in run.messages]


def key_of(item: PlannedMessage) -> str:
    assert item.request is not None
    return item.request.message_key


def make_request(**overrides: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "message_key": "0badc0de",
        "subject": "Budget",
        "sender": EmailContact(name="Alex", address="alex@example.com"),
        "received_at_utc": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "timezone_name": ZONE,
        "body_text": BODY,
    }
    values.update(overrides)
    return AnalysisRequest.model_validate(values)


async def test_cache_hit_reuses_the_analysis_without_provider_calls(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 1)
    first = await analyze(session, FakeAIProvider([answer_all()]), shortlist, account_id=account_id)
    provider = FakeAIProvider()

    rerun = await analyze(session, provider, shortlist, account_id=account_id)

    assert outcomes(first) == [ANALYZED]
    assert provider.calls == 0
    assert rerun.calls == 0
    (item,) = rerun.messages
    assert item.outcome is AnalysisOutcome.REUSED
    assert item.analysis is not None
    assert item.analysis.message_key == key_of(item)
    assert item.analysis_row_id == first.messages[0].analysis_row_id


def test_input_hash_ignores_the_message_key() -> None:
    first = input_hash(make_request(message_key="aaaaaaaa"))

    assert first == input_hash(make_request(message_key="bbbbbbbb"))
    assert re.fullmatch(r"[0-9a-f]{64}", first)


@pytest.mark.parametrize(
    "change",
    [
        {"body_text": f"{BODY} Thanks."},
        {"timezone_name": "Europe/London"},
        {"body_truncated": True},
    ],
    ids=["body", "timezone", "truncation"],
)
def test_input_hash_changes_with_the_content_sent(change: dict[str, object]) -> None:
    assert input_hash(make_request(**change)) != input_hash(make_request())


async def test_seven_messages_go_in_batches_of_five_then_two(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 7)
    provider = FakeAIProvider([answer_all(), answer_all()])

    run = await analyze(session, provider, shortlist, account_id=account_id)

    assert [len(batch) for batch in provider.batches] == [5, 2]
    sent = [request.message_key for batch in provider.batches for request in batch]
    assert sent == [key_of(item) for item in run.messages]
    assert outcomes(run) == [ANALYZED] * 7
    assert run.calls == 2


def unknown_key(requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
    return AnalysisResponse(
        candidates=(
            good_candidate(requests[0]),
            good_candidate(requests[1], message_key="ffffffff"),
        )
    )


def duplicate_key(requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
    return AnalysisResponse(candidates=(good_candidate(requests[0]), good_candidate(requests[0])))


@pytest.mark.parametrize(
    "unusable",
    [
        unknown_key,
        duplicate_key,
        *(AnalysisResponse(problem=problem) for problem in AnalysisProblem),
    ],
    ids=["unknown-key", "duplicate-key", *(problem.value for problem in AnalysisProblem)],
)
async def test_an_unusable_batch_is_retried_one_message_at_a_time(
    session: AsyncSession, unusable: ScriptItem
) -> None:
    account_id, shortlist = await seed(session, 2)
    provider = FakeAIProvider([unusable, answer_all(), answer_all()])

    run = await analyze(session, provider, shortlist, account_id=account_id)

    assert [len(batch) for batch in provider.batches] == [2, 1, 1]
    assert outcomes(run) == [ANALYZED, ANALYZED]


@pytest.mark.parametrize("problem", ["missing", "invalid"])
async def test_a_missing_or_invalid_candidate_gets_one_retry_then_fails(
    session: AsyncSession, problem: str
) -> None:
    account_id, shortlist = await seed(session, 2)

    def bad_answer(request: AnalysisRequest) -> tuple[object, ...]:
        if problem == "missing":
            return ()
        return (good_candidate(request, evidence="Nothing like this was ever written."),)

    provider = FakeAIProvider(
        [
            lambda requests: AnalysisResponse.model_validate(
                {"candidates": (good_candidate(requests[0]), *bad_answer(requests[1]))}
            ),
            lambda requests: AnalysisResponse.model_validate(
                {"candidates": bad_answer(requests[0])}
            ),
        ]
    )

    run = await analyze(session, provider, shortlist, account_id=account_id)

    assert provider.calls == 2
    assert [len(batch) for batch in provider.batches] == [2, 1]
    assert outcomes(run) == [ANALYZED, FAILED]


async def test_a_single_message_call_is_never_retried(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 1)
    provider = FakeAIProvider([AnalysisResponse(problem=AnalysisProblem.REFUSED)])

    run = await analyze(session, provider, shortlist, account_id=account_id)

    assert provider.calls == 1
    assert outcomes(run) == [FAILED]


async def test_a_provider_error_keeps_committed_results_and_fails_the_rest(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "analysis.sqlite3")
    await database.create_schema_for_tests()
    provider = FakeAIProvider([answer_all(), ProviderRateLimitError("slow down")])
    try:
        async with database.session() as session:
            account_id, shortlist = await seed(session, 3)
            run = await analyze(session, provider, shortlist, account_id=account_id, batch_size=1)
            row_ids = [item.message_row_id for item in run.messages]
            async with database.session() as second:
                repo = AnalysisRepository(second)
                stored = [
                    len(await repo.get_by_message_id(row_id))
                    for row_id in row_ids
                    if row_id is not None
                ]
    finally:
        await database.dispose()

    assert outcomes(run) == [ANALYZED, FAILED, FAILED]
    assert run.error_code == "AI_RATE_LIMITED"
    assert provider.calls == run.calls == 2
    assert stored == [1, 0, 0]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AIAuthenticationError("bad key"), "AI_AUTH_FAILED"),
        (AuthenticationRequiredError("sign in"), "AI_AUTH_FAILED"),
        (ProviderPermissionError("denied"), "AI_PERMISSION_DENIED"),
        (ProviderRateLimitError("slow down"), "AI_RATE_LIMITED"),
        (ProviderTimeoutError("too slow"), "AI_TIMEOUT"),
        (ProviderResponseError("odd reply"), "AI_PROVIDER_ERROR"),
        (ProviderUnavailableError("down"), "AI_SERVER_ERROR"),
        (ProviderError("unreachable"), "AI_NETWORK_ERROR"),
    ],
)
async def test_provider_errors_stop_the_run_with_a_code(
    session: AsyncSession, error: ProviderError, code: str
) -> None:
    account_id, shortlist = await seed(session, 2)
    provider = FakeAIProvider([error])

    run = await analyze(session, provider, shortlist, account_id=account_id)

    assert run.error_code == code
    assert outcomes(run) == [FAILED, FAILED]
    assert provider.calls == run.calls == 1


async def test_an_unreadable_groq_reply_fails_every_sent_message(
    session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    account_id, shortlist = await seed(session, 2)
    respx_mock.post(CHAT_URL).respond(
        200, text="<html>maintenance</html>", headers={"content-type": "text/html"}
    )

    async with groq_provider(Settings(groq_model="test-model"), key_store=groq_key_store()) as ai:
        run = await analyze(session, ai, shortlist, account_id=account_id)

    assert run.error_code == "AI_SERVER_ERROR"
    assert outcomes(run) == [FAILED, FAILED]
    assert run.calls == 1


def groq_key_store() -> GroqKeyStore:
    return GroqKeyStore(MemoryVault({(SERVICE, ENTRY): TEST_KEY}))


@pytest.mark.parametrize(
    ("rejected_single", "expected", "error_code"),
    [
        (None, [ANALYZED, ANALYZED, ANALYZED], None),
        (1, [ANALYZED, FAILED, ANALYZED], "AI_REQUEST_REJECTED"),
    ],
    ids=["all-singles-pass", "one-single-rejected"],
)
async def test_a_rejected_batch_is_retried_one_message_at_a_time(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    rejected_single: int | None,
    expected: list[AnalysisOutcome],
    error_code: str | None,
) -> None:
    account_id, shortlist = await seed(session, 3)

    def reject_batches(request: httpx.Request) -> httpx.Response:
        sent = sent_messages(request)
        rejected = len(sent) > 1 or (
            rejected_single is not None and f"Reference {rejected_single}." in sent[0]["body"]
        )
        if rejected:
            return httpx.Response(400, json=error_body("invalid_prompt"))
        return answer_every_message(request)

    route = respx_mock.post(CHAT_URL).mock(side_effect=reject_batches)

    async with groq_provider(Settings(groq_model="test-model"), key_store=groq_key_store()) as ai:
        run = await analyze(session, ai, shortlist, account_id=account_id)

    assert outcomes(run) == expected
    assert run.error_code == error_code
    assert run.calls == route.call_count == 4


async def test_a_rejected_single_message_fails_alone(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 2)
    provider = FakeAIProvider([ProviderRequestRejectedError("no"), answer_all()])

    run = await analyze(session, provider, shortlist, account_id=account_id, batch_size=1)

    assert outcomes(run) == [FAILED, ANALYZED]
    assert run.error_code == "AI_REQUEST_REJECTED"
    assert provider.calls == run.calls == 2


async def test_a_stopping_error_wins_over_an_earlier_rejection(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 3)
    provider = FakeAIProvider([ProviderRequestRejectedError("no"), ProviderTimeoutError("slow")])

    run = await analyze(session, provider, shortlist, account_id=account_id, batch_size=1)

    assert outcomes(run) == [FAILED, FAILED, FAILED]
    assert run.error_code == "AI_TIMEOUT"
    assert provider.calls == 2


async def test_unexpected_errors_propagate(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 1)

    with pytest.raises(RuntimeError):
        await analyze(
            session, FakeAIProvider([RuntimeError("bug")]), shortlist, account_id=account_id
        )


async def test_cancel_between_batches_leaves_unsent_messages_open(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 4)
    cancel = asyncio.Event()

    def answer_then_cancel(requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
        cancel.set()
        return answer_all()(requests)

    provider = FakeAIProvider([answer_then_cancel])

    run = await analyze(
        session, provider, shortlist, account_id=account_id, batch_size=2, cancel=cancel
    )

    assert run.cancelled
    assert provider.calls == 1
    assert outcomes(run) == [ANALYZED, ANALYZED, None, None]


async def test_cancel_before_the_first_call_sends_nothing(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 2)
    cancel = asyncio.Event()
    cancel.set()
    provider = FakeAIProvider()

    run = await analyze(session, provider, shortlist, account_id=account_id, cancel=cancel)

    assert run.cancelled
    assert provider.calls == 0
    assert outcomes(run) == [None, None]


async def test_usage_is_summed_across_calls(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 2)
    provider = FakeAIProvider(
        [
            answer_all(AIUsage(input_tokens=100, output_tokens=20)),
            answer_all(AIUsage(input_tokens=50)),
        ]
    )

    run = await analyze(session, provider, shortlist, account_id=account_id, batch_size=1)

    assert run.usage == AIUsage(input_tokens=150, output_tokens=20)


async def test_usage_is_none_when_no_call_reports_it(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 1)

    run = await analyze(session, FakeAIProvider([answer_all()]), shortlist, account_id=account_id)

    assert run.usage is None


async def test_requests_are_minimized_and_keys_are_random_hex(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 3)
    provider = FakeAIProvider([answer_all()])
    service = AnalysisService(session, provider)

    plan = await service.plan(
        account_id=account_id, shortlist=shortlist, bodies=ready(shortlist), timezone_name=ZONE
    )
    await service.execute(plan)

    (batch,) = provider.batches
    assert service.provider_name == "fake"
    assert service.model_name == "fake-model"
    for request, ranked in zip(batch, shortlist, strict=True):
        dump = request.model_dump_json()
        message = ranked.message
        private = [message.provider_message_id, str(message.web_link), OWNER]
        private.extend(recipient.address for recipient in message.to_recipients)
        assert re.fullmatch(r"[0-9a-f]{8}", request.message_key)
        assert not [value for value in private if value in dump]


async def test_plan_classifies_bodies_without_calling_the_provider(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 4)
    ids = [item.message.provider_message_id for item in shortlist]
    bodies = [
        PreparedBody(provider_message_id=ids[0], status=BodyStatus.EMPTY),
        PreparedBody(provider_message_id=ids[1], status=BodyStatus.UNAVAILABLE),
        PreparedBody(provider_message_id=ids[2], status=BodyStatus.FAILED),
        *ready(shortlist[3:]),
    ]
    provider = FakeAIProvider()

    plan = await AnalysisService(session, provider).plan(
        account_id=account_id, shortlist=shortlist, bodies=bodies, timezone_name=ZONE
    )

    skipped = AnalysisOutcome.SKIPPED
    assert [item.outcome for item in plan.messages] == [skipped, skipped, FAILED, None]
    assert plan.to_send == [plan.messages[3]]
    assert provider.calls == 0


async def test_a_message_without_a_stored_row_fails(session: AsyncSession) -> None:
    account_id, _ = await seed(session, 0)
    shortlist = [RankedMessage(message=make_message(provider_message_id="ghost"), score=5)]

    plan = await AnalysisService(session, FakeAIProvider()).plan(
        account_id=account_id, shortlist=shortlist, bodies=ready(shortlist), timezone_name=ZONE
    )

    assert [item.outcome for item in plan.messages] == [FAILED]


@pytest.mark.parametrize("mismatch", ["missing", "extra", "duplicate-body", "duplicate-message"])
async def test_plan_rejects_bodies_that_do_not_match_the_shortlist(
    session: AsyncSession, mismatch: str
) -> None:
    account_id, shortlist = await seed(session, 2)
    bodies = ready(shortlist)
    if mismatch == "missing":
        bodies = bodies[:1]
    elif mismatch == "extra":
        bodies += ready([RankedMessage(message=make_message(provider_message_id="other"), score=1)])
    elif mismatch == "duplicate-body":
        bodies = [bodies[0], bodies[0]]
    else:
        shortlist = [shortlist[0], shortlist[0]]
        bodies = [bodies[0], bodies[0]]

    with pytest.raises(ValueError, match="match the shortlist"):
        await AnalysisService(session, FakeAIProvider()).plan(
            account_id=account_id, shortlist=shortlist, bodies=bodies, timezone_name=ZONE
        )


async def test_a_cached_row_that_no_longer_validates_is_a_miss(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 1)
    await analyze(session, FakeAIProvider([answer_all()]), shortlist, account_id=account_id)
    await session.execute(update(AnalysisTable).values(deadline_text="Friday"))
    await session.commit()
    provider = FakeAIProvider([answer_all()])

    run = await analyze(session, provider, shortlist, account_id=account_id)

    assert provider.calls == 1
    assert outcomes(run) == [ANALYZED]


async def test_progress_counts_calls_and_survives_a_failing_callback(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    account_id, shortlist = await seed(session, 2)
    events: list[SyncProgress] = []

    def record(update: SyncProgress) -> None:
        events.append(update)
        raise RuntimeError("callback detail")

    caplog.set_level(logging.WARNING)
    run = await analyze(
        session,
        FakeAIProvider([answer_all(), answer_all()]),
        shortlist,
        account_id=account_id,
        batch_size=1,
        progress=record,
    )

    assert [(event.stage, event.ai_batches_completed) for event in events] == [
        (SyncStage.ANALYZING, 1),
        (SyncStage.ANALYZING, 2),
    ]
    assert outcomes(run) == [ANALYZED, ANALYZED]
    assert "Progress callback raised RuntimeError" in caplog.text
    assert "callback detail" not in caplog.text


async def test_colliding_keys_are_regenerated(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 2)
    keys = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])

    plan = await AnalysisService(session, FakeAIProvider(), key_factory=lambda: next(keys)).plan(
        account_id=account_id, shortlist=shortlist, bodies=ready(shortlist), timezone_name=ZONE
    )

    assert [key_of(item) for item in plan.messages] == ["aaaaaaaa", "bbbbbbbb"]


async def test_a_key_factory_that_never_varies_is_rejected(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 2)
    service = AnalysisService(session, FakeAIProvider(), key_factory=lambda: "aaaaaaaa")

    with pytest.raises(RuntimeError, match="unique message key"):
        await service.plan(
            account_id=account_id, shortlist=shortlist, bodies=ready(shortlist), timezone_name=ZONE
        )


@pytest.mark.parametrize("batch_size", [0, 11])
def test_batch_size_must_be_between_one_and_ten(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        AnalysisService(cast(AsyncSession, object()), FakeAIProvider(), batch_size=batch_size)


def test_validate_candidate_trims_quoted_evidence_and_blank_actions() -> None:
    request = make_request()
    quoted = "“Please approve the quarterly budget…”"

    analysis = validate_candidate(
        good_candidate(request, evidence=quoted, action_text="  "), request
    )
    plain = validate_candidate(good_candidate(request, evidence='"Please approve"...'), request)

    assert analysis.evidence == "Please approve the quarterly budget"
    assert analysis.action_text is None
    assert plain.evidence == "Please approve"


async def test_reprs_leave_out_email_text(session: AsyncSession) -> None:
    account_id, shortlist = await seed(session, 1)
    run = await analyze(
        session,
        FakeAIProvider([answer_all(deadline_text="Friday 5 PM", action_text="Approve it")]),
        shortlist,
        account_id=account_id,
    )
    (item,) = run.messages
    assert item.request is not None and item.analysis is not None
    candidate = good_candidate(item.request)

    texts = [repr(item), repr(item.request), repr(item.analysis), repr(candidate)]

    for text in texts:
        for private in ("quarterly budget", "Budget item 0", "A short summary", "Approve it"):
            assert private not in text


def test_validate_candidate_shortens_a_long_summary_and_action() -> None:
    request = make_request()
    exact = "s" * 240
    candidate = good_candidate(
        request,
        summary="word " * 60,
        action_required=True,
        action_text="step " * 250,
    )

    analysis = validate_candidate(candidate, request)
    unchanged = validate_candidate(good_candidate(request, summary=exact), request)

    assert len(analysis.summary) <= 240
    assert analysis.summary.endswith("word…")
    assert analysis.action_text is not None
    assert len(analysis.action_text) <= 1_000
    assert analysis.action_text.endswith("step…")
    assert unchanged.summary == exact


def test_validate_candidate_keeps_evidence_strict_about_length() -> None:
    long_body = "Please approve the budget. " * 50
    request = make_request(body_text=long_body)

    with pytest.raises(ValueError):
        validate_candidate(good_candidate(request, evidence=long_body[:1_001]), request)


def test_validate_candidate_resolves_the_deadline() -> None:
    request = make_request()
    candidate = good_candidate(
        request,
        category="deadline",
        deadline_text="Friday 5 PM",
        deadline_date="2026-09-04",
        deadline_time="17:00",
    )

    analysis = validate_candidate(candidate, request)

    assert analysis.deadline_precision is DeadlinePrecision.DATETIME
    assert analysis.deadline_at_utc == datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    assert analysis.message_key == request.message_key


def test_validate_candidate_keeps_the_analysis_when_the_deadline_date_is_unusable() -> None:
    request = make_request()
    candidate = good_candidate(
        request,
        category="deadline",
        deadline_text="Friday 5 PM",
        deadline_date="Sept 4",
        deadline_time="17:00",
    )

    analysis = validate_candidate(candidate, request)

    assert analysis.deadline_precision is DeadlinePrecision.UNRESOLVED
    assert (analysis.deadline_text, analysis.deadline_date) == ("Friday 5 PM", None)
    assert analysis.summary == "A short summary."


@pytest.mark.parametrize(
    "overrides",
    [
        {"message_key": "ffffffff"},
        {"evidence": "Words that never appeared."},
        {"deadline_text": "next Tuesday"},
        {"category": "deadline"},
        {"action_required": True},
    ],
    ids=["key", "evidence", "deadline", "category", "action"],
)
def test_validate_candidate_rejects_unsupported_output(overrides: dict[str, object]) -> None:
    request = make_request()

    with pytest.raises(ValueError) as caught:
        validate_candidate(good_candidate(request, **overrides), request)

    assert BODY[:20] not in str(caught.value)
    assert "Words that never" not in str(caught.value)
