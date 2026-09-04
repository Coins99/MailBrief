"""Unit tests for shared HTTP retry policy and execution engine."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from mailbrief.infra.http_retry import (
    ResponseVerdict,
    RetryPolicy,
    RetryTracker,
    VerdictKind,
    classify_openai_response,
    current_retry_tracker,
    execute_with_retry,
    parse_openai_ratelimit_reset,
    parse_retry_delay,
)
from mailbrief.ports.errors import (
    AIAuthenticationError,
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)


def _simple_parse_retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _simple_classify(response: httpx.Response, client_request_id: str) -> ResponseVerdict:
    if 200 <= response.status_code < 300:
        return ResponseVerdict(VerdictKind.SUCCESS)
    if response.status_code == 403:
        return ResponseVerdict(
            VerdictKind.FAIL,
            exception=ProviderPermissionError("Forbidden", client_request_id=client_request_id),
        )
    if response.status_code == 429:
        delay = _simple_parse_retry_after(response.headers.get("Retry-After"))
        return ResponseVerdict(
            VerdictKind.RETRY,
            exception=ProviderRateLimitError(
                "Rate limited",
                retry_after_seconds=delay,
                client_request_id=client_request_id,
            ),
            retry_delay=delay,
        )
    if response.status_code in {500, 502, 503, 504}:
        return ResponseVerdict(
            VerdictKind.RETRY,
            exception=ProviderError(
                f"HTTP {response.status_code}",
                client_request_id=client_request_id,
            ),
            retry_delay=None,
        )
    return ResponseVerdict(
        VerdictKind.FAIL,
        exception=ProviderError(f"HTTP {response.status_code}", client_request_id=client_request_id),
    )


@respx.mock
async def test_execute_with_retry_stamps_timeout_and_injects_header() -> None:
    captured_requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    respx.get("https://example.com/api").mock(side_effect=handle)

    policy = RetryPolicy(
        parse_retry_after=_simple_parse_retry_after,
        classify_response=_simple_classify,
        client_request_id_header="client-request-id",
        connect_timeout=10.0,
        read_timeout=30.0,
    )

    async with httpx.AsyncClient() as client:
        result = await execute_with_retry(
            client,
            lambda: httpx.Request("GET", "https://example.com/api"),
            policy=policy,
            client_request_id="client-req-1",
        )

    assert result.response.status_code == 200
    assert result.attempts == 1
    assert result.accumulated_sleep_seconds == 0.0
    assert len(captured_requests) == 1
    assert captured_requests[0].headers["client-request-id"] == "client-req-1"
    assert captured_requests[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 30.0,
        "write": 10.0,
        "pool": 10.0,
    }


@respx.mock
async def test_execute_with_retry_accumulates_sleep_and_retries_transient_failures() -> None:
    respx.get("https://example.com/api").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"status": "success"}),
        ]
    )

    policy = RetryPolicy(
        parse_retry_after=_simple_parse_retry_after,
        classify_response=_simple_classify,
        transient_delays=(1.0, 2.0, 4.0),
    )

    retry_events: list[tuple[int, float]] = []
    with patch("mailbrief.infra.http_retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        async with httpx.AsyncClient() as client:
            result = await execute_with_retry(
                client,
                lambda: httpx.Request("GET", "https://example.com/api"),
                policy=policy,
                client_request_id="client-req-2",
                on_retry=lambda attempt, delay: retry_events.append((attempt, delay)),
            )

    assert result.response.status_code == 200
    assert result.attempts == 2
    assert result.accumulated_sleep_seconds == 1.0
    mock_sleep.assert_awaited_once_with(1.0)
    assert retry_events == [(1, 1.0)]


@respx.mock
async def test_execute_with_retry_sleep_reported_on_failure_exception() -> None:
    respx.get("https://example.com/api").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
        ]
    )

    policy = RetryPolicy(
        parse_retry_after=_simple_parse_retry_after,
        classify_response=_simple_classify,
        max_retries=1,
        transient_delays=(1.5,),
    )

    with patch("mailbrief.infra.http_retry.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as exc_info:
                await execute_with_retry(
                    client,
                    lambda: httpx.Request("GET", "https://example.com/api"),
                    policy=policy,
                    client_request_id="client-req-fail",
                )

    assert exc_info.value.accumulated_sleep_seconds == 1.5
    assert exc_info.value.client_request_id == "client-req-fail"


@respx.mock
async def test_execute_with_retry_budget_floor_raises_before_dispatch() -> None:
    route = respx.get("https://example.com/api").respond(200)

    policy = RetryPolicy(
        parse_retry_after=_simple_parse_retry_after,
        classify_response=_simple_classify,
        request_deadline_seconds=1.5,
        min_remaining_budget_seconds=2.0,
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderTimeoutError) as exc_info:
            await execute_with_retry(
                client,
                lambda: httpx.Request("GET", "https://example.com/api"),
                policy=policy,
                client_request_id="client-req-3",
            )

    assert not route.called
    assert exc_info.value.client_request_id == "client-req-3"
    assert exc_info.value.accumulated_sleep_seconds == 0.0


@respx.mock
async def test_execute_with_retry_non_retryable_classification_raises_immediately() -> None:
    respx.get("https://example.com/api").respond(403)

    policy = RetryPolicy(
        parse_retry_after=_simple_parse_retry_after,
        classify_response=_simple_classify,
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderPermissionError) as exc_info:
            await execute_with_retry(
                client,
                lambda: httpx.Request("GET", "https://example.com/api"),
                policy=policy,
                client_request_id="client-req-4",
            )

    assert exc_info.value.client_request_id == "client-req-4"
    assert exc_info.value.accumulated_sleep_seconds == 0.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", None),
        ("   ", None),
        (None, None),
        ("abc", None),
        ("-5", None),
        ("inf", None),
        ("+inf", None),
        ("-inf", None),
        ("nan", None),
        ("1e5", None),
        ("1E5", None),
        ("1e-2", None),
        ("86401", None),  # > 24h absurd input
        ("25h", None),  # > 24h absurd duration
        ("0", 0.0),
        ("5", 5.0),
        ("1.5", 1.5),
        ("300", 300.0),
        ("6m0s", 360.0),
        ("500ms", 0.5),
        ("1h2m3s", 3723.0),
        ("1h", 3600.0),
        ("2m", 120.0),
        ("45s", 45.0),
    ],
)
def test_parse_retry_delay_table(header: str | None, expected: float | None) -> None:
    result = parse_retry_delay(header)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_parse_openai_ratelimit_reset_prefers_retry_after() -> None:
    headers = {
        "retry-after": "12",
        "x-ratelimit-reset-requests": "100ms",
        "x-ratelimit-reset-tokens": "200ms",
    }
    assert parse_openai_ratelimit_reset(headers) == 12.0


def test_parse_openai_ratelimit_reset_binding_constraints() -> None:
    # Tokens exhausted -> pick reset-tokens
    headers_token = {
        "x-ratelimit-remaining-requests": "100",
        "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-requests": "5s",
        "x-ratelimit-reset-tokens": "12s",
    }
    assert parse_openai_ratelimit_reset(headers_token) == 12.0

    # Requests exhausted -> pick reset-requests
    headers_req = {
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-remaining-tokens": "1000",
        "x-ratelimit-reset-requests": "15s",
        "x-ratelimit-reset-tokens": "2s",
    }
    assert parse_openai_ratelimit_reset(headers_req) == 15.0

    # Both exhausted or neither explicitly 0 -> select max
    headers_both = {
        "x-ratelimit-reset-requests": "6s",
        "x-ratelimit-reset-tokens": "18s",
    }
    assert parse_openai_ratelimit_reset(headers_both) == 18.0


def test_classify_openai_response_success() -> None:
    response = httpx.Response(200, json={"id": "chat-1"})
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.SUCCESS
    assert verdict.exception is None


def test_classify_openai_response_401_unauthorized() -> None:
    response = httpx.Response(
        401,
        json={"error": {"message": "Invalid API key", "code": "invalid_api_key"}},
    )
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.FAIL
    assert isinstance(verdict.exception, AIAuthenticationError)
    assert not isinstance(verdict.exception, AuthenticationRequiredError)
    assert "Invalid API key" in str(verdict.exception)


def test_classify_openai_response_403_forbidden_region() -> None:
    response = httpx.Response(
        403,
        json={"error": {"message": "Country or territory not supported", "type": "unsupported_country_region_error"}},
    )
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.FAIL
    assert isinstance(verdict.exception, ProviderPermissionError)
    assert "not supported" in str(verdict.exception)


def test_classify_openai_response_429_insufficient_quota_is_terminal() -> None:
    response = httpx.Response(
        429,
        json={"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}},
    )
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.FAIL
    assert isinstance(verdict.exception, ProviderPermissionError)
    assert "quota" in str(verdict.exception).lower()


def test_classify_openai_response_429_rate_limit_is_retryable() -> None:
    response = httpx.Response(
        429,
        headers={"x-ratelimit-reset-requests": "15s"},
        json={"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}},
    )
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.RETRY
    assert verdict.retry_delay == 15.0
    assert isinstance(verdict.exception, ProviderRateLimitError)


@pytest.mark.parametrize("status", [408, 409, 500, 502, 503, 504])
def test_classify_openai_response_transient_retries(status: int) -> None:
    response = httpx.Response(status, text="<html>Gateway Error</html>")
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.RETRY
    assert isinstance(verdict.exception, ProviderError)


def test_classify_openai_response_other_4xx_terminal() -> None:
    response = httpx.Response(
        400,
        json={"error": {"message": "Bad request", "code": "invalid_request_error"}},
    )
    verdict = classify_openai_response(response, "client-req")
    assert verdict.kind == VerdictKind.FAIL
    assert isinstance(verdict.exception, ProviderResponseError)


@respx.mock
async def test_execute_with_retry_records_sleep_into_current_retry_tracker() -> None:
    tracker = RetryTracker()
    token = current_retry_tracker.set(tracker)
    try:
        call_count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})

        respx.get("https://example.com/api").mock(side_effect=handle)

        policy = RetryPolicy(
            parse_retry_after=lambda _: 0.01,
            classify_response=lambda resp, cid: ResponseVerdict(VerdictKind.SUCCESS) if resp.status_code == 200 else ResponseVerdict(VerdictKind.RETRY, retry_delay=0.01),
            transient_delays=(0.01,),
        )

        with patch("mailbrief.infra.http_retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            async with httpx.AsyncClient() as client:
                result = await execute_with_retry(
                    client,
                    lambda: httpx.Request("GET", "https://example.com/api"),
                    policy=policy,
                    client_request_id="test-req-sleep",
                )

        assert result.response.status_code == 200
        assert tracker.retry_count == 1
        assert tracker.sleep_seconds == pytest.approx(0.01)
        mock_sleep.assert_awaited_once_with(0.01)
    finally:
        current_retry_tracker.reset(token)
