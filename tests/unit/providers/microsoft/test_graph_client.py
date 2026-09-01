"""Unit tests for the direct Microsoft Graph client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft.auth import MicrosoftAuth
from mailbrief.providers.microsoft.graph_client import (
    GraphClient,
    parse_retry_after,
)


@pytest.fixture
def mock_auth() -> MagicMock:
    auth = MagicMock(spec=MicrosoftAuth)
    auth.get_access_token = AsyncMock(return_value="valid-token-123")
    return auth


def test_parse_retry_after_numeric() -> None:
    assert parse_retry_after("15") == 15.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("invalid") is None


def test_parse_retry_after_http_date() -> None:
    # Future date in RFC 7231 format
    val = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert val is not None
    assert val > 0.0


@respx.mock
async def test_graph_client_get_success(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status_code=200,
        json={"id": "acc-1", "displayName": "Taylor"},
    )

    async with GraphClient(mock_auth) as client:
        data = await client.get("me")

        assert data == {"id": "acc-1", "displayName": "Taylor"}
        assert route.called
        last_request = route.calls.last.request
        assert last_request.headers["Authorization"] == "Bearer valid-token-123"
        assert "client-request-id" in last_request.headers
        assert last_request.headers["return-client-request-id"] == "true"
        assert last_request.headers["User-Agent"] == "MailBrief/0.1.0"


@respx.mock
async def test_graph_client_401_refreshes_and_retries(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(401, json={"error": "token_expired"}),
            httpx.Response(200, json={"id": "acc-1"}),
        ]
    )
    mock_auth.get_access_token = AsyncMock(side_effect=["old-token", "new-token"])

    async with GraphClient(mock_auth) as client:
        data = await client.get("me")

        assert data == {"id": "acc-1"}
        assert route.call_count == 2
        mock_auth.get_access_token.assert_awaited_with(force_refresh=True)


@respx.mock
async def test_graph_client_401_recurrent_raises(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status_code=401,
        json={"error": "invalid_token"},
    )
    mock_auth.get_access_token = AsyncMock(return_value="token")

    async with GraphClient(mock_auth) as client:
        with pytest.raises(AuthenticationRequiredError, match="session expired"):
            await client.get("me")


@respx.mock
async def test_graph_client_429_rate_limiting_retries(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0.01"}),
            httpx.Response(200, json={"id": "acc-1"}),
        ]
    )

    async with GraphClient(mock_auth) as client:
        data = await client.get("me")

        assert data == {"id": "acc-1"}
        assert route.call_count == 2


@respx.mock
async def test_graph_client_429_exceeds_budget_raises(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status_code=429,
        headers={"Retry-After": "120"},
    )

    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderRateLimitError, match="rate limit exceeded") as exc_info:
            await client.get("me")
        assert exc_info.value.retry_after_seconds == 120.0


@respx.mock
async def test_graph_client_5xx_transient_retries(mock_auth: MagicMock) -> None:
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=[
            httpx.Response(503, json={"error": "Service Unavailable"}),
            httpx.Response(200, json={"id": "acc-1"}),
        ]
    )

    async with GraphClient(mock_auth) as client:
        with patch("asyncio.sleep", AsyncMock()):
            data = await client.get("me")

        assert data == {"id": "acc-1"}
        assert route.call_count == 2


@respx.mock
async def test_graph_client_5xx_exhaustion_raises(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status_code=500,
        json={"error": "Internal Server Error"},
    )

    async with GraphClient(mock_auth) as client:
        with (
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(ProviderResponseError, match="server error \\(500\\)"),
        ):
            await client.get("me")


@respx.mock
async def test_graph_client_403_forbidden_raises_permission_error(
    mock_auth: MagicMock,
) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status_code=403,
        json={"error": {"message": "Admin consent required"}},
    )

    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderPermissionError, match="Admin consent required"):
            await client.get("me")


@respx.mock
async def test_graph_client_400_bad_request_raises_response_error(
    mock_auth: MagicMock,
) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").respond(
        status_code=400,
        json={"error": {"message": "Invalid query filter"}},
    )

    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError, match="Invalid query filter"):
            await client.get("me")


async def test_graph_client_untrusted_pagination_host_raises(mock_auth: MagicMock) -> None:
    async with GraphClient(mock_auth) as client:
        with pytest.raises(ProviderResponseError, match="Untrusted Graph pagination host"):
            await client.get("https://evil.com/v1.0/me/messages")


@respx.mock
async def test_graph_client_network_error_retries_and_raises(mock_auth: MagicMock) -> None:
    respx.get("https://graph.microsoft.com/v1.0/me").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    async with GraphClient(mock_auth) as client:
        with (
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(ProviderError, match="Graph network failure"),
        ):
            await client.get("me")
