"""Gmail authentication tests use only synthetic credentials and HTTP responses."""

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from mailbrief.domain.messages import ProviderKind
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.gmail.auth import PROFILE_URL, GmailAuth
from mailbrief.providers.gmail.cache import GmailCredentialStore, RefreshCredential
from mailbrief.providers.gmail.oauth import (
    GMAIL_SCOPE,
    TOKEN_URL,
    AuthorizationGrant,
    DesktopClient,
)
from tests.unit.providers.gmail.test_cache import MemoryVault


class FakeAuthorizer:
    def __init__(self) -> None:
        self.calls = 0

    async def authorize(self, client: DesktopClient) -> AuthorizationGrant:
        self.calls += 1
        return AuthorizationGrant("fake-code", "fake-verifier", "http://127.0.0.1:123/callback")


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def make_auth(http: httpx.AsyncClient) -> tuple[GmailAuth, GmailCredentialStore, FakeAuthorizer]:
    store = GmailCredentialStore(MemoryVault())
    authorizer = FakeAuthorizer()
    return (
        GmailAuth(DesktopClient("client", "client-secret"), store, http, authorizer),
        store,
        authorizer,
    )


def seed(store: GmailCredentialStore) -> None:
    store.save(
        RefreshCredential(
            client_id="client",
            email_address="me@example.com",
            refresh_token=SecretStr("old-refresh"),
        )
    )


def token_json(*, refresh: str | None = "new-refresh") -> dict[str, object]:
    result: dict[str, object] = {
        "access_token": "access-secret",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": GMAIL_SCOPE,
    }
    if refresh:
        result["refresh_token"] = refresh
    return result


async def test_interactive_then_restart_is_silent(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, browser = make_auth(http)
    token = respx_mock.post(TOKEN_URL).respond(json=token_json())
    profile = respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    account = await auth.connect()
    assert account.provider is ProviderKind.GMAIL
    form = parse_qs(token.calls[0].request.content.decode())
    assert form["code_verifier"] == ["fake-verifier"]
    assert form["grant_type"] == ["authorization_code"]
    assert profile.calls[0].request.headers["Authorization"] == "Bearer access-secret"
    assert browser.calls == 1
    assert await auth.access_token() == "access-secret"
    assert token.call_count == 1
    restored = GmailAuth(DesktopClient("client", "client-secret"), store, http, browser)
    assert await restored.connect(silent_only=True) == account
    assert parse_qs(token.calls[1].request.content.decode())["grant_type"] == ["refresh_token"]
    assert browser.calls == 1


async def test_silent_without_cache_never_opens_browser(http: httpx.AsyncClient) -> None:
    auth, _, browser = make_auth(http)
    with pytest.raises(AuthenticationRequiredError, match="No restorable"):
        await auth.connect(silent_only=True)
    assert browser.calls == 0


async def test_refresh_preserves_missing_refresh_token(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, _ = make_auth(http)
    seed(store)
    respx_mock.post(TOKEN_URL).respond(json=token_json(refresh=None))
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    await auth.connect(silent_only=True)
    cached = store.load()
    assert cached and cached.refresh_token.get_secret_value() == "old-refresh"


async def test_expired_access_refreshes_under_lock(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, browser = make_auth(http)
    seed(store)
    token = respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    await auth.connect(silent_only=True)
    auth._expires_at = 0
    values = await asyncio.gather(auth.access_token(), auth.access_token())
    assert all(value == "access-secret" for value in values)
    assert token.call_count == 2
    assert browser.calls == 0


@pytest.mark.parametrize("silent", [True, False])
async def test_revoked_refresh(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter, silent: bool
) -> None:
    auth, store, browser = make_auth(http)
    seed(store)
    route = respx_mock.post(TOKEN_URL)
    route.side_effect = [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(200, json=token_json()),
    ]
    if silent:
        with pytest.raises(AuthenticationRequiredError, match="revoked"):
            await auth.connect(silent_only=True)
        assert browser.calls == 0
    else:
        respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
        await auth.connect()
        assert browser.calls == 1


async def test_wrong_account_does_not_replace_cache(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, _ = make_auth(http)
    seed(store)
    before = store.load()
    respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "other@example.com"})
    with pytest.raises(AuthenticationRequiredError, match="different mailbox"):
        await auth.connect(silent_only=True)
    assert store.load() == before
    assert auth._token is None


async def test_client_change_requires_disconnect(http: httpx.AsyncClient) -> None:
    _, store, browser = make_auth(http)
    seed(store)
    auth = GmailAuth(DesktopClient("other", "secret"), store, http, browser)
    with pytest.raises(ConfigurationError, match="client changed"):
        await auth.connect()
    assert browser.calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**token_json(), "access_token": ""},
        {**token_json(), "expires_in": -1},
        {**token_json(), "token_type": "Other"},
    ],
)
async def test_invalid_token_is_not_saved(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter, payload: dict[str, object]
) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(json=payload)
    with pytest.raises(ProviderResponseError) as error:
        await auth.connect()
    assert "access-secret" not in str(error.value)
    assert store.load() is None


async def test_missing_scope(http: httpx.AsyncClient, respx_mock: respx.MockRouter) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(json={**token_json(), "scope": "other"})
    with pytest.raises(ProviderPermissionError):
        await auth.connect()
    assert store.load() is None


@pytest.mark.parametrize(
    "status,error",
    [
        (401, AuthenticationRequiredError),
        (403, ProviderPermissionError),
        (429, ProviderRateLimitError),
        (500, ProviderResponseError),
        (302, ProviderResponseError),
    ],
)
async def test_status_redaction(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter, status: int, error: type[Exception]
) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(status, text="sensitive-response")
    with pytest.raises(error) as caught:
        await auth.connect()
    assert "sensitive" not in str(caught.value)
    assert store.load() is None


async def test_network_error_does_not_start_interactive_flow(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, browser = make_auth(http)
    seed(store)
    respx_mock.post(TOKEN_URL).side_effect = httpx.ConnectError("sensitive-request")
    with pytest.raises(ProviderResponseError) as error:
        await auth.connect()
    assert "sensitive" not in str(error.value)
    assert browser.calls == 0


@pytest.mark.parametrize("payload", [{}, {"emailAddress": "invalid"}, [], {"emailAddress": 5}])
async def test_invalid_profile(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter, payload: object
) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).respond(json=payload)
    with pytest.raises(ProviderResponseError):
        await auth.connect()
    assert store.load() is None


async def test_offline_access_required(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(json=token_json(refresh=None))
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    with pytest.raises(AuthenticationRequiredError, match="offline"):
        await auth.connect()
    assert store.load() is None


async def test_disconnect_clears_memory_and_storage(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, browser = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    await auth.connect()
    await auth.disconnect()
    await auth.disconnect()
    assert store.load() is None
    assert auth._token is None
    with pytest.raises(AuthenticationRequiredError):
        await auth.access_token()
    assert browser.calls == 1


async def test_reauthorization_cannot_reuse_revoked_refresh(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, _ = make_auth(http)
    seed(store)
    before = store.load()
    respx_mock.post(TOKEN_URL).side_effect = [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(200, json=token_json(refresh=None)),
    ]
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    with pytest.raises(AuthenticationRequiredError, match="offline"):
        await auth.connect()
    assert store.load() == before


async def test_save_failure_does_not_report_connected(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    class UnwritableVault(MemoryVault):
        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError("sensitive-write-error")

    store = GmailCredentialStore(UnwritableVault())
    auth = GmailAuth(DesktopClient("client", "secret"), store, http, FakeAuthorizer())
    respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    with pytest.raises(ConfigurationError, match="OS vault"):
        await auth.connect()
    assert auth._token is None
    assert store.load() is None


async def test_profile_network_failure_is_safe(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).side_effect = httpx.ConnectError("access-secret")
    with pytest.raises(ProviderResponseError) as error:
        await auth.connect()
    assert "access-secret" not in str(error.value)
    assert store.load() is None


async def test_malformed_error_payload_is_safe(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, _, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(400, text="client-secret")
    with pytest.raises(ProviderResponseError) as error:
        await auth.connect()
    assert "client-secret" not in str(error.value)


async def test_refresh_cannot_switch_a_running_sync_to_another_account(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    auth, store, _ = make_auth(http)
    route = respx_mock.post(TOKEN_URL).respond(json=token_json())
    respx_mock.get(PROFILE_URL).respond(json={"emailAddress": "me@example.com"})
    await auth.connect()
    await auth.invalidate_access_token("different-token")
    await auth.access_token()
    assert route.call_count == 1
    await auth.invalidate_access_token("access-secret")
    store.save(
        RefreshCredential(
            client_id="client",
            email_address="other@example.com",
            refresh_token=SecretStr("other-refresh"),
        )
    )
    with pytest.raises(AuthenticationRequiredError, match="account changed"):
        await auth.access_token()
    assert route.call_count == 1


@pytest.mark.parametrize("reason", ["invalid_client", "deleted_client", "unauthorized_client"])
@pytest.mark.parametrize("status", [400, 401])
async def test_client_rejection_gives_safe_setup_guidance(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter, reason: str, status: int
) -> None:
    auth, store, _ = make_auth(http)
    respx_mock.post(TOKEN_URL).respond(
        status, json={"error": reason, "error_description": "private-client-secret"}
    )
    with pytest.raises(ConfigurationError, match="Desktop app") as caught:
        await auth.connect()
    assert "private" not in str(caught.value)
    assert store.load() is None


@pytest.mark.parametrize("stage", ["token exchange", "token refresh", "Gmail profile check"])
@pytest.mark.parametrize("status", [400, 502])
async def test_failure_identifies_request_stage_without_payload(
    http: httpx.AsyncClient, respx_mock: respx.MockRouter, stage: str, status: int
) -> None:
    auth, store, _ = make_auth(http)
    if stage == "token refresh":
        seed(store)
    before = store.load()
    if stage == "Gmail profile check":
        respx_mock.post(TOKEN_URL).respond(json=token_json())
        respx_mock.get(PROFILE_URL).respond(status, text="private-access-token")
    else:
        respx_mock.post(TOKEN_URL).respond(status, json={"error": "private-unknown-reason"})
    with pytest.raises(ProviderResponseError) as caught:
        await auth.connect()
    message = str(caught.value)
    assert stage in message
    assert f"HTTP {status}" in message
    assert "private" not in message
    assert store.load() == before
