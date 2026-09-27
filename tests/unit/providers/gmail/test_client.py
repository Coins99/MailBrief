"""Transport retries, permission handling and metadata-only request boundaries."""

import asyncio

import httpx
import pytest
import respx

from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.gmail.client import (
    MAX_RESPONSE_BYTES,
    MESSAGES_URL,
    GmailClient,
    retry_delay,
)
from tests.unit.providers.gmail.metadata_fixtures import FakeSession, metadata, no_sleep


async def test_metadata_request_is_read_only(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(MESSAGES_URL + "/a1").respond(json=metadata())
    async with httpx.AsyncClient() as http:
        assert await GmailClient(http, FakeSession()).metadata("a1") == metadata()
    request = route.calls[0].request
    assert request.url.params["format"] == "metadata"
    assert "body" not in request.url.params["fields"]
    assert "raw" not in request.url.params["fields"]
    assert request.headers["Authorization"] == "Bearer fake-token"


@pytest.mark.parametrize(
    "payload",
    [
        {"resultSizeEstimate": 0},
        {"messages": [{"id": "a1"}], "nextPageToken": "next", "resultSizeEstimate": 2},
    ],
)
async def test_empty_partial_list_is_rechecked_without_fields(
    respx_mock: respx.MockRouter, payload: dict[str, object]
) -> None:
    route = respx_mock.get(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(204),
            httpx.Response(200, json=payload),
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await GmailClient(http, FakeSession()).list_messages("after:123", "page-1")
    assert result == payload
    first, second = (call.request.url.params for call in route.calls)
    assert "resultSizeEstimate" in first["fields"]
    assert "fields" not in second
    assert first.remove("fields") == second
    assert second["q"] == "after:123"
    assert second["pageToken"] == "page-1"


async def test_repeated_no_content_is_not_an_empty_inbox(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(MESSAGES_URL).respond(204)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError, match="HTTP 204"):
            await GmailClient(http, FakeSession()).list_messages("q", None)
    assert route.call_count == 2


async def test_metadata_no_content_does_not_trigger_unfiltered_download(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(MESSAGES_URL + "/a1").respond(204)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError, match="HTTP 204"):
            await GmailClient(http, FakeSession()).metadata("a1")
    assert route.call_count == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retry_recovers(respx_mock: respx.MockRouter, status: int) -> None:
    route = respx_mock.get(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(status, headers={"Retry-After": "0"}),
            httpx.Response(200, json={}),
        ]
    )
    async with httpx.AsyncClient() as http:
        assert (
            await GmailClient(http, FakeSession(), sleep=no_sleep).list_messages("after:1", None)
            == {}
        )
    assert route.call_count == 2


async def test_refresh_once_then_fail(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(MESSAGES_URL).respond(401)
    session = FakeSession()
    async with httpx.AsyncClient() as http:
        with pytest.raises(AuthenticationRequiredError):
            await GmailClient(http, session).list_messages("q", None)
    assert route.call_count == 2
    assert session.invalidated == ["fake-token"]


@pytest.mark.parametrize(
    "status,exception",
    [(403, ProviderPermissionError), (302, ProviderResponseError), (400, ProviderResponseError)],
)
async def test_permanent_error_has_no_payload(
    respx_mock: respx.MockRouter, status: int, exception: type[Exception]
) -> None:
    route = respx_mock.get(MESSAGES_URL).respond(
        status, text="private-response", headers={"Location": "https://evil.example"}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(exception) as caught:
            await GmailClient(http, FakeSession()).list_messages("q", None)
    assert "private" not in str(caught.value)
    assert route.call_count == 1


async def test_403_rate_limit_and_retry_after_cap(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(MESSAGES_URL).respond(
        403,
        json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
        headers={"Retry-After": "60"},
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderRateLimitError) as caught:
            await GmailClient(http, FakeSession()).list_messages("q", None)
    assert caught.value.retry_after_seconds == 60
    assert route.call_count == 1


async def test_retry_exhaustion(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(MESSAGES_URL).respond(503)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError):
            await GmailClient(http, FakeSession(), sleep=no_sleep).list_messages("q", None)
    assert route.call_count == 4


async def test_transport_failure_exhaustion(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(MESSAGES_URL).mock(side_effect=httpx.ConnectError("private-token"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError) as caught:
            await GmailClient(http, FakeSession(), sleep=no_sleep).list_messages("q", None)
    assert "private-token" not in str(caught.value)
    assert route.call_count == 4


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (400, b"private-response", "HTTP 400"),
        (200, b"private-response", "unreadable response"),
        (503, b"private-response", "HTTP 503"),
    ],
)
async def test_sync_diagnostic_reports_safe_cause_without_payload_or_query(
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: bytes,
    expected: str,
) -> None:
    respx_mock.get(MESSAGES_URL).respond(status, content=body)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError):
            await GmailClient(http, FakeSession(), sleep=no_sleep).list_messages(
                "private-query", None
            )
    assert "Gmail diagnostic:" in caplog.text
    assert expected in caplog.text
    for secret in ("private-response", "private-query", "fake-token"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "failure,expected",
    [
        (httpx.ConnectError("private-connection"), "network request failed"),
        (httpx.ReadTimeout("private-connection"), "timed out"),
    ],
)
async def test_transport_diagnostic_never_logs_original_exception(
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    expected: str,
) -> None:
    respx_mock.get(MESSAGES_URL).mock(side_effect=failure)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError):
            await GmailClient(http, FakeSession(), sleep=no_sleep).list_messages("q", None)
    assert expected in caplog.text
    assert "private-connection" not in caplog.text


@pytest.mark.parametrize(
    "body",
    [b"[]", b"not-json", b"x" * (MAX_RESPONSE_BYTES + 1)],
    ids=["array", "invalid-json", "oversized"],
)
async def test_invalid_or_oversized_response(respx_mock: respx.MockRouter, body: bytes) -> None:
    respx_mock.get(MESSAGES_URL).respond(content=body)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError):
            await GmailClient(http, FakeSession()).list_messages("q", None)


async def test_disappeared_message_is_not_a_list_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(MESSAGES_URL + "/a1").respond(404)
    async with httpx.AsyncClient() as http:
        assert await GmailClient(http, FakeSession()).metadata("a1") is None
        with pytest.raises(ProviderResponseError):
            await GmailClient(http, FakeSession()).metadata("../../other")


async def test_cancel_during_backoff(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(MESSAGES_URL).respond(429)
    waiting = asyncio.Event()

    async def sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient() as http:
        task = asyncio.create_task(
            GmailClient(http, FakeSession(), sleep=sleep).list_messages("q", None)
        )
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_retry_after_parsing() -> None:
    assert retry_delay("0", 0) == 0
    assert retry_delay("Wed, 01 Jan 2020 00:00:00 GMT", 0) == 0
    for value in ("nan", "inf", "junk", None):
        assert 1 <= retry_delay(value, 0) <= 1.5
