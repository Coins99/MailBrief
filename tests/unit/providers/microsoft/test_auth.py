"""Unit tests for deterministic Microsoft authentication."""

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import msal
import pytest

from mailbrief.domain.messages import ProviderKind
from mailbrief.ports.errors import (
    AuthenticationCancelledError,
    AuthenticationRequiredError,
    ProviderError,
)
from mailbrief.providers.microsoft.auth import DEFAULT_SCOPES, MicrosoftAuth


def _auth(application: MagicMock) -> MicrosoftAuth:
    return MicrosoftAuth(
        "client-id",
        token_cache=msal.SerializableTokenCache(),
        _application=application,
    )


def _client_info(uid: str, utid: str) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps({"uid": uid, "utid": utid}).encode())
    return encoded.decode().rstrip("=")


async def test_get_access_token_selects_only_cached_account() -> None:
    application = MagicMock()
    account = {"home_account_id": "uid.tenant", "username": "user@example.com"}
    application.get_accounts.return_value = [account]
    application.acquire_token_silent_with_error.return_value = {"access_token": "secret-token"}
    auth = _auth(application)

    assert await auth.get_access_token() == "secret-token"
    application.acquire_token_silent_with_error.assert_called_once_with(
        scopes=DEFAULT_SCOPES,
        account=account,
        force_refresh=False,
    )


async def test_multiple_cached_accounts_are_never_selected_by_order() -> None:
    application = MagicMock()
    application.get_accounts.return_value = [
        {"home_account_id": "one.tenant"},
        {"home_account_id": "two.tenant"},
    ]

    with pytest.raises(AuthenticationRequiredError, match="unambiguous"):
        await _auth(application).get_access_token()
    application.acquire_token_silent_with_error.assert_not_called()


async def test_retained_selection_is_matched_across_reloaded_account_objects() -> None:
    application = MagicMock()
    initial = {"home_account_id": "uid.tenant", "username": "user@example.com"}
    reloaded = {**initial, "name": "Reloaded"}
    other = {"home_account_id": "other.tenant", "username": "other@example.com"}
    application.get_accounts.side_effect = [[initial], [other, reloaded]]
    application.acquire_token_silent_with_error.return_value = {"access_token": "token"}
    auth = _auth(application)

    await auth.get_access_token()
    await auth.get_access_token()
    assert auth._active_account == reloaded
    assert application.acquire_token_silent_with_error.call_args.kwargs["account"] == reloaded


async def test_missing_retained_selection_never_switches_to_other_cached_account() -> None:
    application = MagicMock()
    selected = {"home_account_id": "selected.tenant", "username": "a@example.com"}
    replacement = {"home_account_id": "replacement.tenant", "username": "b@example.com"}
    application.get_accounts.side_effect = [[selected], [replacement]]
    application.acquire_token_silent_with_error.return_value = {"access_token": "token-a"}
    auth = _auth(application)

    assert await auth.get_access_token() == "token-a"
    with pytest.raises(AuthenticationRequiredError):
        await auth.get_access_token()
    assert application.acquire_token_silent_with_error.call_count == 1
    assert auth._active_account is None


async def test_interactive_result_selects_client_info_and_removes_stale_account() -> None:
    application = MagicMock()
    stale = {"home_account_id": "old.tenant", "username": "old@example.com"}
    selected = {"home_account_id": "uid.tenant", "username": "new@example.com"}
    application.acquire_token_interactive.return_value = {
        "access_token": "do-not-return",
        "client_info": _client_info("uid", "tenant"),
        "id_token_claims": {"preferred_username": "new@example.com"},
    }
    application.get_accounts.return_value = [stale, selected]
    auth = _auth(application)

    assert await auth.acquire_token_interactive() is None
    application.remove_account.assert_called_once_with(stale)
    assert auth._active_account == selected


async def test_interactive_result_uses_only_unique_username_fallback() -> None:
    application = MagicMock()
    selected = {"home_account_id": "uid.tenant", "username": "User@Example.com"}
    application.acquire_token_interactive.return_value = {
        "access_token": "token",
        "client_info": "malformed",
        "id_token_claims": {"upn": "user@example.com"},
    }
    application.get_accounts.return_value = [selected]
    auth = _auth(application)

    await auth.acquire_token_interactive()
    assert auth._active_account == selected


@pytest.mark.parametrize("client_info", [None, "malformed", _client_info("", "tenant")])
async def test_ambiguous_interactive_selection_removes_nothing(client_info: str | None) -> None:
    application = MagicMock()
    accounts = [
        {"home_account_id": "one.tenant", "username": "same@example.com"},
        {"home_account_id": "two.tenant", "username": "same@example.com"},
    ]
    application.acquire_token_interactive.return_value = {
        "access_token": "token",
        "client_info": client_info,
        "id_token_claims": {"preferred_username": "same@example.com"},
    }
    application.get_accounts.return_value = accounts

    with pytest.raises(AuthenticationRequiredError, match="identify the selected"):
        await _auth(application).acquire_token_interactive()
    application.remove_account.assert_not_called()


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required", "consent_required"])
async def test_silent_auth_required_codes_are_typed_and_sanitized(error: str) -> None:
    application = MagicMock()
    application.get_accounts.return_value = [{"home_account_id": "uid.tenant"}]
    application.acquire_token_silent_with_error.return_value = {
        "error": error,
        "error_description": "sensitive provider description",
    }

    with pytest.raises(AuthenticationRequiredError) as exc_info:
        await _auth(application).get_access_token(force_refresh=True)
    assert "sensitive" not in str(exc_info.value)


async def test_silent_unknown_failure_is_provider_error() -> None:
    application = MagicMock()
    application.get_accounts.return_value = [{"home_account_id": "uid.tenant"}]
    application.acquire_token_silent_with_error.return_value = {
        "error": "server_error",
        "error_description": "sensitive provider description",
    }

    with pytest.raises(ProviderError) as exc_info:
        await _auth(application).get_access_token()
    assert not isinstance(exc_info.value, AuthenticationRequiredError)
    assert "sensitive" not in str(exc_info.value)


async def test_silent_none_result_requires_interactive_authentication() -> None:
    application = MagicMock()
    application.get_accounts.return_value = [{"home_account_id": "uid.tenant"}]
    auth = _auth(application)

    application.acquire_token_silent_with_error.return_value = None
    with pytest.raises(AuthenticationRequiredError):
        await auth.get_access_token()


async def test_silent_msal_exception_is_sanitized() -> None:
    application = MagicMock()
    application.get_accounts.return_value = [{"home_account_id": "uid.tenant"}]
    auth = _auth(application)
    application.acquire_token_silent_with_error.side_effect = RuntimeError("private MSAL data")
    with pytest.raises(ProviderError) as exc_info:
        await auth.get_access_token()
    assert "private MSAL" not in str(exc_info.value)


@pytest.mark.parametrize("result", [None, {"error": "access_denied", "error_description": "x"}])
async def test_interactive_cancellation_is_typed(result: object) -> None:
    application = MagicMock()
    application.acquire_token_interactive.return_value = result

    with pytest.raises(AuthenticationCancelledError):
        await _auth(application).acquire_token_interactive()


async def test_interactive_service_failure_does_not_become_auth_required() -> None:
    application = MagicMock()
    application.acquire_token_interactive.return_value = {
        "error": "temporarily_unavailable",
        "error_description": "internal details",
    }

    with pytest.raises(ProviderError) as exc_info:
        await _auth(application).acquire_token_interactive()
    assert not isinstance(exc_info.value, AuthenticationRequiredError)
    assert "internal details" not in str(exc_info.value)


@pytest.mark.parametrize(
    "result",
    [
        "not-a-result",
        {"error": "interaction_required", "error_description": "private"},
    ],
)
async def test_interactive_malformed_and_auth_required_results_are_typed(result: object) -> None:
    application = MagicMock()
    application.acquire_token_interactive.return_value = result
    expected = ProviderError if isinstance(result, str) else AuthenticationRequiredError
    with pytest.raises(expected):
        await _auth(application).acquire_token_interactive()


async def test_cached_identity_uses_home_account_tenant() -> None:
    application = MagicMock()
    application.get_accounts.return_value = [
        {
            "home_account_id": "uid.tenant-id",
            "username": "taylor@example.com",
            "name": "Taylor Smith",
        }
    ]

    identity = await _auth(application).get_account_identity_from_cache()
    assert identity is not None
    assert identity.provider is ProviderKind.MICROSOFT
    assert identity.provider_account_id == "uid.tenant-id"
    assert identity.tenant_id == "tenant-id"


async def test_cached_identity_rejects_malformed_account() -> None:
    application = MagicMock()
    application.get_accounts.return_value = [
        {"home_account_id": "uid.tenant", "username": "not-an-email", "name": None}
    ]
    assert await _auth(application).get_account_identity_from_cache() is None


async def test_cached_identity_handles_empty_and_unreadable_cache() -> None:
    application = MagicMock()
    application.get_accounts.return_value = []
    auth = _auth(application)
    assert await auth.get_account_identity_from_cache() is None

    application.get_accounts.side_effect = RuntimeError("private cache data")
    with pytest.raises(ProviderError) as exc_info:
        await auth.get_account_identity_from_cache()
    assert "private cache" not in str(exc_info.value)


async def test_disconnect_removes_snapshot_and_verifies_empty() -> None:
    application = MagicMock()
    account = {"home_account_id": "uid.tenant"}
    application.get_accounts.side_effect = [[account], []]
    auth = _auth(application)
    auth._active_account = account

    await auth.disconnect()
    application.remove_account.assert_called_once_with(account)
    assert auth._active_account is None


async def test_disconnect_verification_and_msal_failure_are_sanitized() -> None:
    account = {"home_account_id": "uid.tenant"}
    application = MagicMock()
    application.get_accounts.side_effect = [[account], [account]]
    with pytest.raises(ProviderError, match="completely removed"):
        await _auth(application).disconnect()

    application = MagicMock()
    application.get_accounts.side_effect = RuntimeError("private cache data")
    with pytest.raises(ProviderError) as exc_info:
        await _auth(application).disconnect()
    assert "private cache" not in str(exc_info.value)


async def test_async_constructor_builds_application_off_loop() -> None:
    application = MagicMock()
    with patch(
        "mailbrief.providers.microsoft.auth.msal.PublicClientApplication",
        return_value=application,
    ) as constructor:
        auth = await MicrosoftAuth.create(
            "client-id",
            token_cache=msal.SerializableTokenCache(),
        )
    assert auth._app is application
    constructor.assert_called_once()


async def test_async_constructor_failure_is_sanitized() -> None:
    with (
        patch(
            "mailbrief.providers.microsoft.auth.msal.PublicClientApplication",
            side_effect=RuntimeError("private authority response"),
        ),
        pytest.raises(ProviderError) as exc_info,
    ):
        await MicrosoftAuth.create("client-id", token_cache=msal.SerializableTokenCache())
    assert "private authority" not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [base64.urlsafe_b64encode(b"[]").decode(), _client_info("uid", "")],
)
def test_client_info_contract_rejects_non_account_shapes(payload: str) -> None:
    assert MicrosoftAuth._home_account_id_from_result({"client_info": payload}) is None


def test_locked_msal_version_produces_expected_home_account_shape() -> None:
    cache = msal.SerializableTokenCache()
    client_info = _client_info("uid", "tenant")
    cache.add(
        {
            "client_id": "client-id",
            "scope": ["User.Read"],
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "grant_type": "authorization_code",
            "response": {
                "access_token": "token",
                "client_info": client_info,
                "expires_in": 3600,
                "id_token_claims": {
                    "oid": "local-object-id",
                    "preferred_username": "user@example.com",
                },
            },
        }
    )
    application = object.__new__(msal.PublicClientApplication)
    application.token_cache = cache
    application.authority = SimpleNamespace(instance="login.microsoftonline.com")
    application._instance_discovery = False

    accounts = application.get_accounts()
    assert len(accounts) == 1
    assert accounts[0]["home_account_id"] == "uid.tenant"
    assert accounts[0]["username"] == "user@example.com"
    assert (
        MicrosoftAuth._home_account_id_from_result({"client_info": client_info})
        == (accounts[0]["home_account_id"])
    )


async def test_cancelled_msal_worker_holds_lock_until_it_finishes() -> None:
    application = MagicMock()
    account = {"home_account_id": "uid.tenant"}
    started = threading.Event()
    release = threading.Event()

    def slow_acquire(**_kwargs: object) -> dict[str, str]:
        started.set()
        release.wait(timeout=2)
        return {"access_token": "token"}

    application.get_accounts.side_effect = [[account], [], []]
    application.acquire_token_silent_with_error.side_effect = slow_acquire
    auth = _auth(application)

    token_task = asyncio.create_task(auth.get_access_token())
    assert await asyncio.to_thread(started.wait, 1)
    token_task.cancel()
    disconnect_task = asyncio.create_task(auth.disconnect())
    await asyncio.sleep(0.02)
    assert not disconnect_task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await token_task
    await disconnect_task
