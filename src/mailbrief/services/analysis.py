"""Plan, send and validate AI analysis for the reviewed shortlist, caching every result.

Logs carry counts and error codes only; email and candidate text never reach them.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import (
    ACTION_TEXT_MAX_CHARS,
    ANALYSIS_SCHEMA_VERSION,
    EVIDENCE_BODY_SHARE,
    EVIDENCE_STORE_CHARS,
    EVIDENCE_TOTAL_CHARS,
    FOLLOW_UP_EVIDENCE_CHARS,
    MAX_ANALYSIS_BATCH,
    MAX_SUGGESTION_STEPS,
    MAX_SUGGESTIONS,
    SUGGESTION_EVIDENCE_CHARS,
    SUGGESTION_STEP_MAX_CHARS,
    SUGGESTION_TITLE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    ActionCandidate,
    ActionSuggestion,
    AIUsage,
    AnalysisCandidate,
    AnalysisProblem,
    AnalysisRequest,
    AnalysisResponse,
    DeadlinePrecision,
    FollowUpKind,
    MessageAnalysis,
)
from mailbrief.domain.bodies import BodyStatus, PreparedBody
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.digests import SyncProgress, SyncStage
from mailbrief.domain.messages import RankedMessage
from mailbrief.ports.ai_provider import AIProvider
from mailbrief.ports.errors import (
    NETWORK_BLOCKED_CODE,
    AIAuthenticationError,
    AICredentialsMissingError,
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUsageLimitError,
)
from mailbrief.services.deadlines import (
    InvalidDeadlineError,
    ResolvedDeadline,
    resolve_deadline,
    resolve_deadline_fields,
    suggest_target,
)
from mailbrief.storage.repositories import AnalysisRepository, MessageRepository
from mailbrief.text.matching import appears_in
from mailbrief.text.prepare import clean_generated_text, truncate_at_boundary

logger = logging.getLogger(__name__)

_KEY_ATTEMPTS = 64
_QUOTES = "\"'‘’“”"
_ELLIPSES = ("...", "…")
REQUEST_REJECTED = "AI_REQUEST_REJECTED"
OUTPUT_INCOMPLETE = "AI_OUTPUT_INCOMPLETE"
_DETAIL_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
KEY_MISSING = "AI_KEY_MISSING"
_WHITESPACE = re.compile(r"\s+")
_IGNORED_CATEGORIES = ("P", "S")  # Unicode punctuation and symbols.
_NO_DEADLINE = ResolvedDeadline(None, DeadlinePrecision.NONE, None, None, None)


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


def _fit(text: str, limit: int) -> str:
    """Text within ``limit`` characters; longer text is cut at a word break and ends in "…"."""
    if len(text) <= limit:
        return text
    return truncate_at_boundary(text, limit - 1)[0] + "…"


def suggestion_fingerprint(title: str) -> str:
    """SHA-256 hex of a title with case, punctuation, symbols and spacing ignored.

    Raises ValueError, whose message is static, when nothing is left to compare.
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    kept = "".join(
        char for char in folded if not unicodedata.category(char).startswith(_IGNORED_CATEGORIES)
    )
    normalized = _WHITESPACE.sub(" ", kept).strip()
    if not normalized:
        raise ValueError("a suggestion title needs letters or digits")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_candidate(candidate: AnalysisCandidate, request: AnalysisRequest) -> MessageAnalysis:
    """Check an untrusted candidate against its request and build the validated analysis.

    An over-long summary or action is shortened. The evidence must quote the email, and is
    stored at most 300 characters long and never as the whole body: it is cut to under 80%
    of the body. The follow-up signal is checked next by _follow_up, and becomes NONE
    rather than failing the message. Suggested actions are checked one at a time by
    _suggestions, which drops a bad one without failing the message; they get what is left
    of the evidence budget. Raises ValueError (pydantic's ValidationError is one); messages
    never echo email text.
    """
    if candidate.message_key != request.message_key:
        raise ValueError("the candidate key does not match its request")
    evidence = _trim_evidence(candidate.evidence)
    if not appears_in(evidence, request.subject, request.body_text):
        raise ValueError("evidence must quote the email")
    # Strictly under the share: a 100-character body stores at most 79 characters.
    share_cap = math.ceil(len(request.body_text) * EVIDENCE_BODY_SHARE) - 1
    evidence_cap = min(EVIDENCE_STORE_CHARS, share_cap)
    if evidence_cap < 2:
        raise ValueError("the body is too short to store any evidence from it")
    stored_evidence = _fit(evidence, evidence_cap)
    deadline = resolve_deadline(candidate, request)
    action_text = clean_generated_text(candidate.action_text or "")
    budget = min(EVIDENCE_TOTAL_CHARS, share_cap) - len(stored_evidence)
    follow_up, follow_up_evidence = _follow_up(candidate, request, deadline, budget)
    if follow_up_evidence is not None:
        budget -= len(follow_up_evidence)
    return MessageAnalysis(
        message_key=request.message_key,
        category=candidate.category,
        summary=_fit(clean_generated_text(candidate.summary), SUMMARY_MAX_CHARS),
        action_required=candidate.action_required,
        action_text=_fit(action_text, ACTION_TEXT_MAX_CHARS) if action_text else None,
        deadline_text=deadline.text,
        deadline_precision=deadline.precision,
        deadline_date=deadline.date,
        deadline_at_utc=deadline.at_utc,
        deadline_timezone=deadline.timezone,
        confidence=candidate.confidence,
        evidence=stored_evidence,
        suggestions=_suggestions(candidate.actions, request, evidence_budget=budget),
        follow_up=follow_up,
        follow_up_evidence=follow_up_evidence,
    )


def _follow_up(
    candidate: AnalysisCandidate,
    request: AnalysisRequest,
    deadline: ResolvedDeadline,
    evidence_budget: int,
) -> tuple[FollowUpKind, str | None]:
    """The candidate's follow-up signal and its quote, or (NONE, None) (ADR 0016).

    The signal is kept only when its trimmed quote is 2 to 160 characters, quotes the email
    and fits ``evidence_budget``, and, for a new deadline, when the email states a deadline.
    """
    kind = candidate.follow_up
    if kind is FollowUpKind.NONE or candidate.follow_up_evidence is None:
        return FollowUpKind.NONE, None
    quote = _trim_evidence(candidate.follow_up_evidence)
    if (
        not 2 <= len(quote) <= min(FOLLOW_UP_EVIDENCE_CHARS, evidence_budget)
        or not appears_in(quote, request.subject, request.body_text)
        or (kind is FollowUpKind.NEW_DEADLINE and deadline.precision is DeadlinePrecision.NONE)
    ):
        return FollowUpKind.NONE, None
    return kind, quote


def _suggestions(
    actions: Sequence[ActionCandidate], request: AnalysisRequest, *, evidence_budget: int
) -> tuple[ActionSuggestion, ...]:
    """Up to five suggestions from the candidate actions, in their order.

    An action is skipped when its title has no letters or digits, repeats a kept title
    once case, punctuation and spacing are ignored, or its evidence does not quote the
    email. A deadline that does not check out is dropped and the action kept. A quote is
    stored only while ``evidence_budget`` characters remain, so the message's evidence
    stays within its total; otherwise the suggestion keeps no evidence.
    """
    kept: list[ActionSuggestion] = []
    used: set[str] = set()
    for action in actions:
        if len(kept) == MAX_SUGGESTIONS:
            break
        title = _fit(clean_generated_text(action.title), SUGGESTION_TITLE_MAX_CHARS)
        try:
            fingerprint = suggestion_fingerprint(title)
        except ValueError:
            continue
        evidence = _trim_evidence(action.evidence)
        if fingerprint in used or not appears_in(evidence, request.subject, request.body_text):
            continue
        try:
            deadline = resolve_deadline_fields(
                action.deadline_text,
                action.deadline_date,
                action.deadline_time,
                action.stated_timezone,
                request,
            )
        except InvalidDeadlineError:
            deadline = _NO_DEADLINE
        stripped = (clean_generated_text(step) for step in action.steps)
        steps = tuple(_fit(step, SUGGESTION_STEP_MAX_CHARS) for step in stripped if step)
        target_date, target_reason = suggest_target(deadline, request)
        quote = _fit(evidence, SUGGESTION_EVIDENCE_CHARS)
        quote_fits = len(quote) <= evidence_budget  # Otherwise the suggestion keeps none.
        if quote_fits:
            evidence_budget -= len(quote)
        kept.append(
            ActionSuggestion(
                position=len(kept),
                title=title,
                ownership=action.ownership,
                effort=action.effort,
                deadline_text=deadline.text,
                deadline_precision=deadline.precision,
                deadline_date=deadline.date,
                deadline_at_utc=deadline.at_utc,
                deadline_timezone=deadline.timezone,
                suggested_target_date=target_date,
                target_reason=target_reason,
                steps=steps[:MAX_SUGGESTION_STEPS],
                evidence=quote if quote_fits else None,
                fingerprint=fingerprint,
            )
        )
        used.add(fingerprint)
    return tuple(kept)


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

    # The message, request and analysis carry email text, so they stay out of the repr.
    ranked: RankedMessage = field(repr=False)
    message_row_id: int | None
    request: AnalysisRequest | None = field(repr=False)
    input_hash: str | None
    outcome: AnalysisOutcome | None = None
    analysis: MessageAnalysis | None = field(default=None, repr=False)
    analysis_row_id: int | None = None


@dataclass(slots=True)
class AnalysisPlan:
    """Every shortlisted message, in shortlist order."""

    messages: list[PlannedMessage]

    @property
    def to_send(self) -> list[PlannedMessage]:
        """Messages that still need a provider result, in shortlist order."""
        return [message for message in self.messages if message.outcome is None]

    def defer_after(self, limit: int) -> int:
        """Keep the first ``limit`` messages of ``to_send`` and defer the rest; returns how
        many were deferred.

        The shortlist is in rank order, so the lowest-ranked messages are the ones deferred.
        A deferred message has an outcome, so it is never sent, never cached and never in the
        brief; only the coverage counts it. Messages that already have an outcome, such as
        cached analyses, cost nothing and are untouched.
        """
        if limit < 0:
            raise ValueError("limit cannot be negative")
        over = self.to_send[limit:]
        for message in over:
            message.outcome = AnalysisOutcome.DEFERRED
        return len(over)


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    """The executed plan with usage, call counts, cancellation and an error code.

    ``calls`` counts logical provider calls; ``requests_sent`` counts the HTTP attempts they
    made, including failed and retried ones. ``error_code`` is the code of the error that
    stopped the run; otherwise AI_REQUEST_REJECTED when a rejected request left a message
    out; otherwise AI_OUTPUT_INCOMPLETE when a message failed and an answer was cut off;
    otherwise None.
    """

    messages: tuple[PlannedMessage, ...]
    usage: AIUsage | None
    calls: int
    cancelled: bool
    error_code: str | None
    requests_sent: int = 0
    provider_detail: str | None = None  # See provider_detail(); set with error_code.


@dataclass(slots=True)
class _Tally:
    calls: int = 0
    rejected: int = 0  # Messages left out because the provider rejected their request.
    rejected_detail: str | None = None  # The first such rejection's provider detail.
    incomplete: int = 0  # Answers the provider couldn't finish, usually at the output limit.
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

    def __init__(self, error_code: str, detail: str | None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.detail = detail


def provider_detail(exc: ProviderError) -> str | None:
    """The HTTP status and sanitized provider code behind an error, never provider text.

    For example "HTTP 403, code permission_denied"; None when neither is known.
    """
    parts: list[str] = []
    status = exc.http_status
    if status is not None and 100 <= status <= 599:
        parts.append(f"HTTP {status}")
    code = exc.provider_error_code
    if code is not None and _DETAIL_CODE_PATTERN.fullmatch(code):
        parts.append(f"code {code}")
    return ", ".join(parts) or None


def provider_error_code(exc: ProviderError) -> str:
    """The AI_* code for an expected provider error; shared with AI drafting."""
    if isinstance(exc, ProviderUsageLimitError):
        return "AI_USAGE_LIMIT"
    if isinstance(exc, AICredentialsMissingError):
        return KEY_MISSING
    if isinstance(exc, AIAuthenticationError | AuthenticationRequiredError):
        return "AI_AUTH_FAILED"
    if isinstance(exc, ProviderPermissionError):
        if exc.provider_error_code == NETWORK_BLOCKED_CODE:
            return "AI_NETWORK_BLOCKED"
        return "AI_PERMISSION_DENIED"
    if isinstance(exc, ProviderRateLimitError):
        return "AI_RATE_LIMITED"
    if isinstance(exc, ProviderTimeoutError):
        return "AI_TIMEOUT"
    if isinstance(exc, ProviderUnavailableError):
        return "AI_SERVER_ERROR"
    if isinstance(exc, ProviderRequestRejectedError):
        return REQUEST_REJECTED
    if isinstance(exc, ProviderResponseError):
        return "AI_PROVIDER_ERROR"
    return "AI_NETWORK_ERROR"


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
        if not 1 <= batch_size <= MAX_ANALYSIS_BATCH:
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

    @property
    def privacy_notice(self) -> str:
        return self._provider.privacy_notice

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
        suggestions = await self._analyses.get_suggestions(cached.id)
        try:
            planned.analysis = AnalysisRepository.to_domain(
                cached, message_key=request.message_key, suggestions=suggestions
            )
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

    async def credentials_available(self) -> bool:
        """Whether the provider has a usable API key; loading it sends nothing."""
        return await self._provider.credentials_available()

    def fail_unsent(self, plan: AnalysisPlan, error_code: str) -> AnalysisRun:
        """Fail every message still waiting for a result without calling the provider."""
        unsent = plan.to_send
        _fail(unsent)
        logger.info("AI analysis skipped for %d messages: %s", len(unsent), error_code)
        return AnalysisRun(
            messages=tuple(plan.messages),
            usage=None,
            calls=0,
            cancelled=False,
            error_code=error_code,
        )

    async def execute(
        self,
        plan: AnalysisPlan,
        *,
        cancel: asyncio.Event | None = None,
        progress: Callable[[SyncProgress], None] | None = None,
    ) -> AnalysisRun:
        """Send unresolved messages in batches, cache valid results and retry misses alone.

        No message gets more than two attempts. Messages missing from a batch's usable
        answer are retried alone at once. A single-message call whose answer is unusable is
        retried once more, after every message has had its first attempt. A rejected
        request is retried one message at a time, and a rejected single message fails
        without a retry. Cancellation is checked before every provider call and leaves
        unsent messages without an outcome. Any other expected provider error stops all
        further calls and fails every message still without an outcome; other exceptions
        propagate.
        """
        pending = plan.to_send
        requests_before = self._provider.requests_sent
        tally = _Tally()
        cancelled = False
        error_code: str | None = None
        detail: str | None = None
        deferred: list[PlannedMessage] = []  # Single-message first attempts to try once more.
        try:
            for start in range(0, len(pending), self._batch_size):
                batch = pending[start : start + self._batch_size]
                retry = await self._call(batch, tally, cancel, progress)
                if len(batch) == 1:
                    deferred.extend(retry)
                    continue
                for item in retry:
                    _fail(await self._call([item], tally, cancel, progress))
            for item in deferred:  # Second and last attempts, after every first attempt.
                _fail(await self._call([item], tally, cancel, progress))
        except _Cancelled:
            cancelled = True
        except _ProviderStopped as stop:
            error_code = stop.error_code
            detail = stop.detail
            _fail(item for item in plan.messages if item.outcome is None)
        analyzed = sum(item.outcome is AnalysisOutcome.ANALYZED for item in plan.messages)
        failed = sum(item.outcome is AnalysisOutcome.FAILED for item in plan.messages)
        if error_code is None and tally.rejected:
            error_code = REQUEST_REJECTED
            detail = tally.rejected_detail
        elif error_code is None and tally.incomplete and failed:
            error_code = OUTPUT_INCOMPLETE
        requests_sent = self._provider.requests_sent - requests_before
        logger.info(
            "AI analysis: %d calls, %d requests, %d analyzed, %d failed, cancelled=%s, error=%s",
            tally.calls,
            requests_sent,
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
            requests_sent=requests_sent,
            provider_detail=detail,
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
            code = provider_error_code(exc)
            # INFO: the CLI already reports the failure and its detail to the owner.
            logger.info("AI provider call failed: %s", code)
            if code != REQUEST_REJECTED:
                raise _ProviderStopped(code, provider_detail(exc)) from None
            if len(batch) > 1:
                retry = list(batch)  # Find the refused message by sending one at a time.
            else:
                tally.rejected += 1  # A refused single message is left out, never resent.
                if tally.rejected_detail is None:
                    tally.rejected_detail = provider_detail(exc)
                _fail(batch)
                retry = []
        else:
            tally.add(response.usage)
            if response.problem is AnalysisProblem.INCOMPLETE:
                tally.incomplete += 1
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
