"""Shared HTTP retry policy, execution engine, duration parsing, and response classification."""

import asyncio
import email.utils
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import httpx

from mailbrief.ports.errors import (
    NETWORK_BLOCKED_CODE,
    AIAuthenticationError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(
    r"^(?=[0-9])(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m(?!s))?(?:(?P<seconds>\d+(?:\.\d+)?)s)?(?:(?P<ms>\d+(?:\.\d+)?)ms)?$"
)
_DELTA_SECONDS_RE = re.compile(r"^\d+(\.\d+)?$")
# Groq's 403 text for a blocked network, matched case-insensitively (see classify_groq_response).
_GROQ_NETWORK_BLOCK = "check your network settings"
MAX_REASONABLE_DELAY_SECONDS = 86400.0  # 24 hours ceiling to reject absurd inputs


def parse_retry_delay(header_value: str | None) -> float | None:
    """Parse nonnegative delta-seconds, duration string, or HTTP-date without arbitrary lower clamping.

    Rejects absurd delays (> 24 hours), non-finite floats, exponential notation, and negative numbers.
    """  # noqa: E501
    if header_value is None:
        return None

    val = header_value.strip()
    if not val:
        return None

    # 1. Delta-seconds (pure decimal digits, no scientific notation or non-finite values)
    if _DELTA_SECONDS_RE.fullmatch(val):
        try:
            seconds = float(val)
            if math.isfinite(seconds) and 0.0 <= seconds <= MAX_REASONABLE_DELAY_SECONDS:
                return max(0.0, seconds)
            return None
        except (ValueError, OverflowError):
            return None

    # 2. Duration string (e.g. 6m0s, 500ms, 1h2m3s)
    match = _DURATION_RE.fullmatch(val)
    if match:
        groups = match.groupdict()
        hours = float(groups.get("hours") or 0.0)
        minutes = float(groups.get("minutes") or 0.0)
        seconds = float(groups.get("seconds") or 0.0)
        ms = float(groups.get("ms") or 0.0)
        total = hours * 3600.0 + minutes * 60.0 + seconds + (ms / 1000.0)
        if math.isfinite(total) and 0.0 <= total <= MAX_REASONABLE_DELAY_SECONDS:
            return max(0.0, total)
        return None

    # 3. HTTP date fallback
    try:
        target_dt = email.utils.parsedate_to_datetime(val)
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=UTC)
        delay = (target_dt - datetime.now(UTC)).total_seconds()
        if math.isfinite(delay) and 0.0 <= delay <= MAX_REASONABLE_DELAY_SECONDS:
            return max(0.0, delay)
        return None
    except (TypeError, ValueError, OverflowError):
        pass

    return None


def parse_ai_ratelimit_reset(headers: Mapping[str, str]) -> float | None:
    """Determine retry delay from OpenAI-compatible rate limit headers.

    Prefers Retry-After if present. Otherwise evaluates remaining requests vs tokens
    to select the binding constraint's reset duration.
    """
    normalized = {k.casefold(): v for k, v in headers.items()}

    # 1. Retry-After takes precedence
    retry_after = parse_retry_delay(normalized.get("retry-after"))
    if retry_after is not None:
        return retry_after

    # 2. Check remaining capacities
    rem_requests: int | None = None
    rem_tokens: int | None = None
    raw_rem_req = normalized.get("x-ratelimit-remaining-requests")
    if raw_rem_req is not None:
        try:  # noqa: SIM105
            rem_requests = int(raw_rem_req)
        except ValueError:
            pass
    raw_rem_tok = normalized.get("x-ratelimit-remaining-tokens")
    if raw_rem_tok is not None:
        try:  # noqa: SIM105
            rem_tokens = int(raw_rem_tok)
        except ValueError:
            pass

    reset_requests = parse_retry_delay(normalized.get("x-ratelimit-reset-requests"))
    reset_tokens = parse_retry_delay(normalized.get("x-ratelimit-reset-tokens"))

    # If tokens are exhausted and requests are not, token limit is the binding constraint
    if rem_tokens == 0 and (rem_requests is None or rem_requests > 0):  # noqa: SIM102
        if reset_tokens is not None:
            return reset_tokens

    # If requests are exhausted and tokens are not, request limit is the binding constraint
    if rem_requests == 0 and (rem_tokens is None or rem_tokens > 0):  # noqa: SIM102
        if reset_requests is not None:
            return reset_requests

    # Otherwise, take the maximum of both reset durations
    if reset_tokens is not None and reset_requests is not None:
        return max(reset_tokens, reset_requests)

    return reset_tokens if reset_tokens is not None else reset_requests


class VerdictKind(StrEnum):
    """Outcome classification for an HTTP response."""

    SUCCESS = "success"
    RETRY = "retry"
    FAIL = "fail"


@dataclass(frozen=True)
class ResponseVerdict:
    """Classification verdict returned by a provider response classifier."""

    kind: VerdictKind
    exception: Exception | None = None
    retry_delay: float | None = None


def classify_groq_response(response: httpx.Response, client_request_id: str) -> ResponseVerdict:
    """Classify Groq failures without propagating provider text or malformed error fields."""
    status = response.status_code
    if response.is_success:
        return ResponseVerdict(VerdictKind.SUCCESS)
    code: str | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        candidate = body["error"].get("code")
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", candidate):
            code = candidate
        message = body["error"].get("message")
        # Groq's edge blocks many VPN, proxy and data-centre networks with a 403 that carries
        # no code, before the key is checked. Its text is only matched here, never kept.
        if status == 403 and isinstance(message, str) and _GROQ_NETWORK_BLOCK in message.casefold():
            code = NETWORK_BLOCKED_CODE
    kind: type[ProviderError] = ProviderResponseError
    retry = False
    delay = None
    if status == 401:
        kind = AIAuthenticationError
    elif status == 403 or code in {"blocked_api_access", "insufficient_quota"}:
        kind = ProviderPermissionError
    elif status == 429:
        kind = ProviderRateLimitError
        retry = True
        delay = parse_ai_ratelimit_reset(response.headers)
    elif status in {408, 409, 500, 502, 503, 504}:
        retry = True
        delay = parse_retry_delay(response.headers.get("retry-after"))
    return ResponseVerdict(
        VerdictKind.RETRY if retry else VerdictKind.FAIL,
        exception=kind(
            f"Groq request failed (HTTP {status}).",
            client_request_id=client_request_id,
            provider_error_code=code,
        ),
        retry_delay=delay,
    )


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable HTTP retry and classification rules."""

    parse_retry_after: Callable[[str | None], float | None]
    classify_response: Callable[[httpx.Response, str], ResponseVerdict]
    client_request_id_header: str | None = None
    max_retries: int = 3
    request_deadline_seconds: float = 45.0
    min_remaining_budget_seconds: float = 2.0
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    write_timeout: float = 10.0
    pool_timeout: float = 10.0
    transient_delays: tuple[float, ...] = (1.0, 2.0, 4.0)


@dataclass(frozen=True)
class RetryResult:
    """Execution telemetry and returned HTTP response."""

    response: httpx.Response
    attempts: int
    accumulated_sleep_seconds: float


@dataclass
class RetryTracker:
    """Accumulates retry attempts and sleep durations across operations."""

    sleep_seconds: float = 0.0
    retry_count: int = 0

    def record_sleep(self, delay: float) -> None:
        self.sleep_seconds += delay
        self.retry_count += 1


current_retry_tracker: ContextVar[RetryTracker | None] = ContextVar(
    "current_retry_tracker", default=None
)


async def execute_with_retry(
    client: httpx.AsyncClient,
    build_request: Callable[[], httpx.Request],
    *,
    policy: RetryPolicy,
    client_request_id: str,
    on_retry: Callable[[int, float], None] | None = None,
) -> RetryResult:
    """Execute a single logical request with wall-clock deadline, timeout clamping, and sleep accounting."""  # noqa: E501
    deadline = time.monotonic() + policy.request_deadline_seconds
    attempt = 0
    accumulated_sleep = 0.0

    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining < policy.min_remaining_budget_seconds:
            raise ProviderTimeoutError(
                "HTTP request deadline exceeded.",
                client_request_id=client_request_id,
                accumulated_sleep_seconds=accumulated_sleep,
            )

        connect_to = min(policy.connect_timeout, remaining)
        read_to = min(policy.read_timeout, remaining)

        request = build_request()
        if policy.client_request_id_header:
            request.headers[policy.client_request_id_header] = client_request_id

        # Stamp timeout extensions inside the engine so callers cannot forget:
        request.extensions["timeout"] = {
            "connect": connect_to,
            "read": read_to,
            "write": policy.write_timeout,
            "pool": policy.pool_timeout,
        }

        try:
            response = await client.send(
                request,
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            delay = (
                policy.transient_delays[attempt]
                if attempt < len(policy.transient_delays)
                else policy.transient_delays[-1]
            )
            if not math.isfinite(delay) or delay < 0.0:
                delay = 1.0
            delay = max(0.0, delay)

            if attempt < policy.max_retries and (time.monotonic() + delay) <= deadline:
                attempt += 1
                if on_retry is not None:
                    try:
                        on_retry(attempt, delay)
                    except Exception as cb_exc:
                        logger.warning("on_retry callback raised an exception: %s", cb_exc)
                tracker = current_retry_tracker.get()
                if tracker is not None:
                    tracker.record_sleep(delay)
                await asyncio.sleep(delay)
                accumulated_sleep += delay
                continue
            raise ProviderError(
                "HTTP network request failed.",
                client_request_id=client_request_id,
                accumulated_sleep_seconds=accumulated_sleep,
            ) from exc

        verdict = policy.classify_response(response, client_request_id)
        if verdict.kind == VerdictKind.SUCCESS:
            return RetryResult(
                response=response,
                attempts=attempt + 1,
                accumulated_sleep_seconds=accumulated_sleep,
            )

        exc_to_raise = verdict.exception or ProviderError(
            f"HTTP request failed with status {response.status_code}.",
            client_request_id=client_request_id,
        )
        exc_to_raise.accumulated_sleep_seconds = accumulated_sleep  # type: ignore[attr-defined]

        if verdict.kind == VerdictKind.RETRY:
            raw_delay = (
                verdict.retry_delay
                if verdict.retry_delay is not None
                else (
                    policy.transient_delays[attempt]
                    if attempt < len(policy.transient_delays)
                    else policy.transient_delays[-1]
                )
            )
            if not math.isfinite(raw_delay) or raw_delay < 0.0:
                raw_delay = (
                    policy.transient_delays[attempt]
                    if attempt < len(policy.transient_delays)
                    else policy.transient_delays[-1]
                )
            delay = max(0.0, raw_delay)

            if attempt < policy.max_retries and (time.monotonic() + delay) <= deadline:
                attempt += 1
                if on_retry is not None:
                    try:
                        on_retry(attempt, delay)
                    except Exception as cb_exc:
                        logger.warning("on_retry callback raised an exception: %s", cb_exc)
                tracker = current_retry_tracker.get()
                if tracker is not None:
                    tracker.record_sleep(delay)
                await asyncio.sleep(delay)
                accumulated_sleep += delay
                continue

        raise exc_to_raise
