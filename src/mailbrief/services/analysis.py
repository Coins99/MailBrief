"""Plan, send and validate AI analysis for the reviewed shortlist, caching every result.

Logs carry counts and error codes only; email and candidate text never reach them.
"""

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import (
    ANALYSIS_SCHEMA_VERSION,
    AIUsage,
    AnalysisCandidate,
    AnalysisRequest,
    AnalysisResponse,
    MessageAnalysis,
)
from mailbrief.domain.bodies import BodyStatus, PreparedBody
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.digests import SyncProgress, SyncStage
from mailbrief.domain.messages import RankedMessage
from mailbrief.ports.ai_provider import AIProvider
from mailbrief.ports.errors import (
    AIAuthenticationError,
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUsageLimitError,
)
from mailbrief.services.deadlines import resolve_deadline
from mailbrief.storage.repositories import AnalysisRepository, MessageRepository
from mailbrief.text.matching import appears_in

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 10
_KEY_ATTEMPTS = 64
_QUOTES = "\"'‘’“”"
_ELLIPSES = ("...", "…")


def _random_key() -> str:
    return secrets.token_hex(4)


def input_hash(request: AnalysisRequest) -> str:
    """SHA-256 of everything sent for one message except its per-run message key."""
    payload = {
        "subject": request.subject,
        "sender_name": request.sender.name,
        "sender_address": request.sender.address,
        "received_at_utc": request.received_at_utc.isoformat(),
        "timezone_name": request.timezone_name,
        "body_text": request.body_text,
        "body_truncated": request.body_truncated,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _trim_evidence(evidence: str) -> str:
    """Drop the surrounding quotes and trailing ellipsis that providers add when quoting."""
    text = evidence.strip().strip(_QUOTES).strip()
    for ellipsis in _ELLIPSES:
        if text.endswith(ellipsis):
            return text.removesuffix(ellipsis).rstrip().rstrip(_QUOTES).rstrip()
    return text


def validate_candidate(candidate: AnalysisCandidate, request: AnalysisRequest) -> MessageAnalysis:
    """Check an untrusted candidate against its request and build the validated analysis.

    Raises ValueError (pydantic's ValidationError is one); messages never echo email text.
    """
    if candidate.message_key != request.message_key:
        raise ValueError("the candidate key does not match its request")
    evidence = _trim_evidence(candidate.evidence)
    if not appears_in(evidence, request.subject, request.body_text):
        raise ValueError("evidence must quote the email")
    deadline = resolve_deadline(candidate, request)
    return MessageAnalysis(
        message_key=request.message_key,
        category=candidate.category,
        summary=candidate.summary,
        action_required=candidate.action_required,
        action_text=(candidate.action_text or "").strip() or None,
        deadline_text=deadline.text,
        deadline_precision=deadline.precision,
        deadline_date=deadline.date,
        deadline_at_utc=deadline.at_utc,
        deadline_timezone=deadline.timezone,
        confidence=candidate.confidence,
        evidence=evidence,
    )


def emit_progress(
    progress: Callable[[SyncProgress], None] | None,
    update: SyncProgress,
) -> None:
    """Report progress; a failing callback is logged by type and never stops the run."""
    if progress is None:
        return
    try:
        progress(update)
    except Exception as exc:
        logger.warning("Progress callback raised %s", type(exc).__name__)


@dataclass(slots=True)
class PlannedMessage:
    """One shortlisted message and what happened to it in this run."""

    ranked: RankedMessage
    message_row_id: int | None
    request: AnalysisRequest | None
    input_hash: str | None
    outcome: AnalysisOutcome | None = None
    analysis: MessageAnalysis | None = None
    analysis_row_id: int | None = None


@dataclass(slots=True)
class AnalysisPlan:
    """Every shortlisted message, in shortlist order."""

    messages: list[PlannedMessage]

    @property
    def to_send(self) -> list[PlannedMessage]:
        """Messages that still need a provider result, in shortlist order."""
        return [message for message in self.messages if message.outcome is None]


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    """The executed plan with usage, call count, cancellation and the first error code."""

    messages: tuple[PlannedMessage, ...]
    usage: AIUsage | None
    calls: int
    cancelled: bool
    error_code: str | None


@dataclass(slots=True)
class _Tally:
    calls: int = 0
    reported: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None

    def add(self, usage: AIUsage | None) -> None:
        if usage is None:
            return
        self.reported = True
        if usage.input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + usage.output_tokens

    def usage(self) -> AIUsage | None:
        if not self.reported:
            return None
        return AIUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens)


class _Cancelled(Exception):
    """Cancellation was requested before a provider call."""


class _ProviderStopped(Exception):
    """The provider raised an expected error, so no further calls are made."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _error_code(exc: ProviderError) -> str:
    if isinstance(exc, ProviderUsageLimitError):
        return "AI_USAGE_LIMIT"
    if isinstance(exc, AIAuthenticationError | AuthenticationRequiredError):
        return "AI_AUTH_FAILED"
    if isinstance(exc, ProviderPermissionError):
        return "AI_PERMISSION_DENIED"
    if isinstance(exc, ProviderRateLimitError):
        return "AI_RATE_LIMITED"
    if isinstance(exc, ProviderTimeoutError):
        return "AI_TIMEOUT"
    return "AI_PROVIDER_ERROR"


def _request(item: PlannedMessage) -> AnalysisRequest:
    if item.request is None:
        raise RuntimeError("only planned requests can be sent")
    return item.request


def _fail(items: Iterable[PlannedMessage]) -> None:
    for item in items:
        item.outcome = AnalysisOutcome.FAILED


class AnalysisService:
    """Cached, validated AI analysis of one run's shortlist."""

    def __init__(
        self,
        session: AsyncSession,
        provider: AIProvider,
        *,
        batch_size: int = 5,
        key_factory: Callable[[], str] = _random_key,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError("batch_size must be between 1 and 10")
        self._session = session
        self._provider = provider
        self._batch_size = batch_size
        self._key_factory = key_factory
        self._analyses = AnalysisRepository(session)
        self._messages = MessageRepository(session)

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    async def plan(
        self,
        *,
        account_id: int,
        shortlist: Sequence[RankedMessage],
        bodies: Sequence[PreparedBody],
        timezone_name: str,
    ) -> AnalysisPlan:
        """Classify every shortlisted message and reuse valid cached results; no provider call."""
        prepared = {body.provider_message_id: body for body in bodies}
        wanted = [item.message.provider_message_id for item in shortlist]
        if (
            len(prepared) != len(bodies)
            or len(set(wanted)) != len(wanted)
            or prepared.keys() != set(wanted)
        ):
            raise ValueError("prepared bodies must match the shortlist one to one")
        used_keys: set[str] = set()
        messages = [
            await self._plan_one(
                account_id,
                ranked,
                prepared[ranked.message.provider_message_id],
                timezone_name,
                used_keys,
            )
            for ranked in shortlist
        ]
        return AnalysisPlan(messages)

    async def _plan_one(
        self,
        account_id: int,
        ranked: RankedMessage,
        body: PreparedBody,
        timezone_name: str,
        used_keys: set[str],
    ) -> PlannedMessage:
        if body.status in (BodyStatus.EMPTY, BodyStatus.UNAVAILABLE):
            return PlannedMessage(ranked, None, None, None, AnalysisOutcome.SKIPPED)
        if body.status is BodyStatus.FAILED:
            return PlannedMessage(ranked, None, None, None, AnalysisOutcome.FAILED)
        message = ranked.message
        request = AnalysisRequest(
            message_key=self._unique_key(used_keys),
            subject=message.subject,
            sender=message.sender,
            received_at_utc=message.received_at_utc,
            timezone_name=timezone_name,
            body_text=body.text,
            body_truncated=body.truncated,
        )
        request_hash = input_hash(request)
        row = await self._messages.get_by_provider_message_id(
            account_id, message.provider_message_id
        )
        if row is None:
            return PlannedMessage(ranked, None, request, request_hash, AnalysisOutcome.FAILED)
        planned = PlannedMessage(ranked, row.id, request, request_hash)
        cached = await self._analyses.get_cached_analysis(
            message_id=row.id,
            input_hash=request_hash,
            provider=self.provider_name,
            model=self.model_name,
            prompt_version=self._provider.prompt_version,
            schema_version=ANALYSIS_SCHEMA_VERSION,
        )
        if cached is None:
            return planned
        try:
            planned.analysis = AnalysisRepository.to_domain(cached, message_key=request.message_key)
        except ValueError:
            return planned  # A cached row that no longer validates counts as a miss.
        planned.outcome = AnalysisOutcome.REUSED
        planned.analysis_row_id = cached.id
        return planned

    def _unique_key(self, used_keys: set[str]) -> str:
        for _ in range(_KEY_ATTEMPTS):
            key = self._key_factory()
            if key not in used_keys:
                used_keys.add(key)
                return key
        raise RuntimeError("could not generate a unique message key")

    async def execute(
        self,
        plan: AnalysisPlan,
        *,
        cancel: asyncio.Event | None = None,
        progress: Callable[[SyncProgress], None] | None = None,
    ) -> AnalysisRun:
        """Send unresolved messages in batches, cache valid results and retry misses alone.

        Cancellation is checked before every provider call and leaves unsent messages
        without an outcome. An expected provider error stops all further calls and fails
        every message still without an outcome; other exceptions propagate.
        """
        pending = plan.to_send
        tally = _Tally()
        cancelled = False
        error_code: str | None = None
        try:
            for start in range(0, len(pending), self._batch_size):
                batch = pending[start : start + self._batch_size]
                retry = await self._call(batch, tally, cancel, progress)
                if len(batch) == 1:
                    _fail(retry)
                    continue
                for item in retry:
                    _fail(await self._call([item], tally, cancel, progress))
        except _Cancelled:
            cancelled = True
        except _ProviderStopped as stop:
            error_code = stop.error_code
            _fail(item for item in plan.messages if item.outcome is None)
        analyzed = sum(item.outcome is AnalysisOutcome.ANALYZED for item in plan.messages)
        failed = sum(item.outcome is AnalysisOutcome.FAILED for item in plan.messages)
        logger.info(
            "AI analysis: %d calls, %d analyzed, %d failed, cancelled=%s, error=%s",
            tally.calls,
            analyzed,
            failed,
            cancelled,
            error_code,
        )
        return AnalysisRun(
            messages=tuple(plan.messages),
            usage=tally.usage(),
            calls=tally.calls,
            cancelled=cancelled,
            error_code=error_code,
        )

    async def _call(
        self,
        batch: Sequence[PlannedMessage],
        tally: _Tally,
        cancel: asyncio.Event | None,
        progress: Callable[[SyncProgress], None] | None,
    ) -> list[PlannedMessage]:
        """Make one provider call, commit its valid results and return the messages to retry."""
        if cancel is not None and cancel.is_set():
            raise _Cancelled
        requests = [_request(item) for item in batch]
        tally.calls += 1
        try:
            response = await self._provider.analyze(requests)
        except ProviderError as exc:
            code = _error_code(exc)
            logger.warning("AI provider call failed: %s", code)
            raise _ProviderStopped(code) from None
        tally.add(response.usage)
        retry = await self._persist(batch, response)
        await self._session.commit()
        emit_progress(
            progress, SyncProgress(stage=SyncStage.ANALYZING, ai_batches_completed=tally.calls)
        )
        return retry

    async def _persist(
        self,
        batch: Sequence[PlannedMessage],
        response: AnalysisResponse,
    ) -> list[PlannedMessage]:
        batch_keys = {_request(item).message_key for item in batch}
        keys = [candidate.message_key for candidate in response.candidates]
        if (
            response.problem is not None
            or len(set(keys)) != len(keys)
            or not set(keys) <= batch_keys
        ):
            logger.info("AI response unusable for a batch of %d", len(batch))
            return list(batch)
        candidates = {candidate.message_key: candidate for candidate in response.candidates}
        retry: list[PlannedMessage] = []
        for item in batch:
            request = _request(item)
            analysis = _validated(candidates.get(request.message_key), request)
            if analysis is None:
                retry.append(item)
                continue
            assert item.message_row_id is not None and item.input_hash is not None
            row = await self._analyses.upsert_analysis(
                message_id=item.message_row_id,
                input_hash=item.input_hash,
                provider=self.provider_name,
                model=self.model_name,
                prompt_version=self._provider.prompt_version,
                schema_version=ANALYSIS_SCHEMA_VERSION,
                analysis=analysis,
            )
            item.analysis = analysis
            item.analysis_row_id = row.id
            item.outcome = AnalysisOutcome.ANALYZED
        if retry:
            logger.info("%d of %d results missing or invalid", len(retry), len(batch))
        return retry


def _validated(
    candidate: AnalysisCandidate | None,
    request: AnalysisRequest,
) -> MessageAnalysis | None:
    if candidate is None:
        return None
    try:
        return validate_candidate(candidate, request)
    except ValueError:
        return None
