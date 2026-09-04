import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from mailbrief import __version__
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from mailbrief.providers.microsoft.auth import MicrosoftAuth
from mailbrief.providers.microsoft.graph_client import GraphClient, parse_retry_after


@pytest.fixture
def mock_auth() -> MagicMock:
    auth = MagicMock(spec=MicrosoftAuth)
    auth.get_access_token = AsyncMock(return_value="valid-token")
    return auth


def test_parse_retry_after_numeric_date_and_invalid_values() -> None:
    assert parse_retry_after("15") == 15.0
    assert parse_retry_after("-2") == 0.0
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0  # type: ignore[operator]
    assert parse_retry_after(None) is None
    assert parse_retry_after("nan") is None
    assert parse_retry_after("invalid") is None


@respx.mock
async def test_get_adds_controlled_headers_and_returns_object(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").respond(
        200,
        json={"id": "account"},
    )
    async with GraphClient(mock_auth) as client:
        assert await client.get("me", headers={"Prefer": "safe"}) == {"id": "account"}

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer valid-token"
    assert request.headers["Prefer"] == "safe"
    assert request.headers["return-client-request-id"] == "true"
    assert request.headers["User-Agent"] == f"MailBrief/{__version__}"
    assert request.extensions["timeout"]["connect"] == 10.0
    assert request.extensions["timeout"]["read"] == 30.0


@respx.mock
async def test_get_with_injected_client_overrides_constructed_timeouts(
    mock_auth: MagicMock,
) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").respond(
        200,
        json={"id": "account"},
    )
    bare_client = httpx.AsyncClient(timeout=5.0)
    async with GraphClient(mock_auth, http_client=bare_client) as client:
        assert await client.get("me") == {"id": "account"}

    request = route.calls.last.request
    assert request.extensions["timeout"]["connect"] == 10.0
    assert request.extensions["timeout"]["read"] == 30.0
    await bare_client.aclose()


@respx.mock
async def test_redirect_is_rejected_and_not_followed(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        302,
        headers={"Location": "https://evil.example/token-target"},
    )
    evil_route = respx.get("https://evil.example/token-target").respond(200, json={})

    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError, match=r"request failed \(302\)"):
            await client.get("me")

    assert not evil_route.called


@pytest.mark.parametrize("header", ["Authorization", "authorization", "USER-AGENT", "Host", "host"])
async def test_reserved_header_override_is_rejected_before_token(
    mock_auth: MagicMock,
    header: str,
) -> None:
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError, match="reserved"):
            await client.get("me", headers={header: "attacker"})
    mock_auth.get_access_token.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1.0/me",
        "//graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com:444/v1.0/me",
        "https://user@graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com/v1.0evil/me",
        "https://graph.microsoft.com/v1.0/%2e%2e/me",
        "https://graph.microsoft.com/v1.0/a%2fb",
        "../me",
    ],
)
async def test_untrusted_urls_are_rejected_before_token(mock_auth: MagicMock, url: str) -> None:
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError, match="Untrusted"):
            await client.get(url)
    mock_auth.get_access_token.assert_not_awaited()


@pytest.mark.parametrize(
    "base_url",
    ["http://graph.microsoft.com/v1.0", "https://graph.microsoft.com", "not-a-url"],
)
def test_invalid_base_url_is_rejected(mock_auth: MagicMock, base_url: str) -> None:
    with pytest.raises(ProviderResponseError, match="base URL"):
        GraphClient(mock_auth, base_url=base_url)


@respx.mock
async def test_continuation_keeps_query_and_discards_caller_params(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque").respond(
        200, json={"value": []}
    )
    continuation = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque"
    async with GraphClient(mock_auth) as client:
        await client.get(continuation, params={"$top": 50})
    assert route.called


@respx.mock
async def test_trusted_relative_path_allows_encoded_message_id(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me/messages/folder%2Fid%20%3F").respond(
        200, json={"body": {}}
    )
    async with GraphClient(mock_auth) as client:
        await client.get("me/messages/folder%2Fid%20%3F")
    assert route.called


@respx.mock
async def test_401_refreshes_once_silently(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"id": "account"})]
    )
    mock_auth.get_access_token = AsyncMock(side_effect=["old", "new"])
    async with GraphClient(mock_auth) as client:
        assert await client.get("me") == {"id": "account"}
    assert route.call_count == 2
    mock_auth.get_access_token.assert_awaited_with(force_refresh=True)


@respx.mock
async def test_401_repeated_requires_authentication(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(401)
    mock_auth.get_access_token = AsyncMock(return_value="token")
    async with GraphClient(mock_auth) as client:
        with pytest.raises(AuthenticationRequiredError):
            await client.get("me")


@respx.mock
async def test_429_retries_when_within_budget(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": "account"}),
        ]
    )
    async with GraphClient(mock_auth) as client:
        assert await client.get("me") == {"id": "account"}
    assert route.call_count == 2


@respx.mock
async def test_429_raises_immediately_when_retry_after_exceeds_deadline(
    mock_auth: MagicMock,
) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        429,
        headers={"Retry-After": "60", "request-id": "srv-throttle-123"},
    )
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await client.get("me", deadline_seconds=45.0)
    assert exc_info.value.retry_after_seconds == 60.0
    assert exc_info.value.server_request_id == "srv-throttle-123"
    assert exc_info.value.client_request_id is not None


@respx.mock
async def test_503_honors_retry_after(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"id": "account"}),
        ]
    )
    sleep = AsyncMock()
    async with GraphClient(mock_auth) as client:
        with patch("mailbrief.providers.microsoft.graph_client.asyncio.sleep", sleep):
            assert await client.get("me") == {"id": "account"}
    assert route.call_count == 2
    assert sleep.await_args_list[0].args[0] == 3.0


@respx.mock
async def test_client_request_id_reused_across_retries(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"id": "account"}),
        ]
    )
    sleep = AsyncMock()
    async with GraphClient(mock_auth) as client:
        with patch("mailbrief.providers.microsoft.graph_client.asyncio.sleep", sleep):
            assert await client.get("me") == {"id": "account"}
    assert route.call_count == 2
    first_req_id = route.calls[0].request.headers["client-request-id"]
    second_req_id = route.calls[1].request.headers["client-request-id"]
    assert first_req_id == second_req_id


@respx.mock
async def test_request_deadline_exceeded_raises_timeout_error(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(200, json={"id": "account"})
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderTimeoutError):
            await client.get("me", deadline_seconds=0.0)


@respx.mock
async def test_transient_server_errors_retry_exact_delays(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(504),
        ]
    )
    sleep = AsyncMock()
    async with GraphClient(mock_auth) as client:
        with patch("mailbrief.providers.microsoft.graph_client.asyncio.sleep", sleep):
            with pytest.raises(ProviderResponseError, match=r"server error \(504\)"):
                await client.get("me")
    assert route.call_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status", [302, 400, 501, 505, 599])
@respx.mock
async def test_every_other_non_2xx_is_rejected(mock_auth: MagicMock, status: int) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status,
        json={"error": {"code": "Bounded.Code", "message": "private details"}},
    )
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError) as exc_info:
            await client.get("me")
    assert "Bounded.Code" in str(exc_info.value)
    assert "private details" not in str(exc_info.value)


@respx.mock
async def test_403_is_permission_error_without_provider_message(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        403,
        json={"error": {"code": "Authorization_RequestDenied", "message": "private"}},
    )
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderPermissionError) as exc_info:
            await client.get("me")
    assert "Authorization_RequestDenied" in str(exc_info.value)
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("status", "content", "content_type"),
    [(204, b"", None), (200, b"not-json", "application/json"), (200, b"[]", "application/json")],
)
@respx.mock
async def test_success_must_be_nonempty_json_object(
    mock_auth: MagicMock,
    status: int,
    content: bytes,
    content_type: str | None,
) -> None:
    headers = {"Content-Type": content_type} if content_type else None
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status,
        content=content,
        headers=headers,
    )
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError):
            await client.get("me")


@respx.mock
async def test_all_transport_errors_retry_and_are_sanitized(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=httpx.ReadError("private URL and query")
    )
    async with GraphClient(mock_auth) as client:
        with patch("mailbrief.providers.microsoft.graph_client.asyncio.sleep", AsyncMock()):
            with pytest.raises(ProviderError) as exc_info:
                await client.get("me")
    assert route.call_count == 4
    assert "private URL" not in str(exc_info.value)


@respx.mock
async def test_cancellation_during_retry_sleep_propagates(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").respond(503)
    sleeping = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    async with GraphClient(mock_auth) as client:
        with patch("mailbrief.providers.microsoft.graph_client.asyncio.sleep", blocked_sleep):
            task = asyncio.create_task(client.get("me"))
            await sleeping.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert route.call_count == 1


async def test_cancellation_during_active_http_request_propagates(mock_auth: MagicMock) -> None:
    started = asyncio.Event()

    async def blocked_request(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(blocked_request))
    async with GraphClient(mock_auth, http_client=http_client) as client:
        task = asyncio.create_task(client.get("me"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await http_client.aclose()


async def test_injected_http_client_retains_ownership(mock_auth: MagicMock) -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    async with GraphClient(mock_auth, http_client=http_client):
        pass
    assert not http_client.is_closed
    await http_client.aclose()


async def test_owned_http_client_is_closed(mock_auth: MagicMock) -> None:
    graph_client = GraphClient(mock_auth)
    owned_client = graph_client._client
    async with graph_client:
        pass
    assert owned_client.is_closed


@respx.mock
async def test_request_budget_floor_raises_before_dispatch(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").respond(200, json={"id": "account"})
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderTimeoutError):
            await client.get("me", deadline_seconds=1.5)
    assert not route.called


@respx.mock
async def test_on_retry_exception_does_not_abort_retry(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"id": "account"}),
        ]
    )

    def exploding_callback(_attempt: int, _delay: float) -> None:
        raise RuntimeError("callback explosion")

    sleep = AsyncMock()
    async with GraphClient(mock_auth) as client:
        with patch("mailbrief.providers.microsoft.graph_client.asyncio.sleep", sleep):
            result = await client.get("me", on_retry=exploding_callback)

    assert result == {"id": "account"}
    assert route.call_count == 2
