"""Retry-delay parsing and Groq response classification."""

import email.utils
import math
import re
from collections.abc import Mapping
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
)

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
