"""Unit tests for the Microsoft authentication adapter."""

from pathlib import Path
from unittest.mock import patch

import msal
import pytest

from mailbrief.domain.messages import ProviderKind
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderPermissionError,
)
from mailbrief.providers.microsoft.auth import (
    DEFAULT_SCOPES,
    MicrosoftAuth,
    get_default_token_cache,
)


@pytest.fixture
def memory_cache() -> msal.SerializableTokenCache:
    """Provide an in-memory serializable token cache."""
    return msal.SerializableTokenCache()


def test_get_default_token_cache_creates_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "tokens.bin"
    cache = get_default_token_cache(cache_path, allow_unencrypted_fallback=True)

    assert isinstance(cache, msal.SerializableTokenCache)


async def test_get_access_token_with_active_account(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)

    mock_account = {"username": "user@example.com", "home_account_id": "acc-1"}
    with (
        patch.object(auth._app, "get_accounts", return_value=[mock_account]),
        patch.object(
            auth._app,
            "acquire_token_silent_with_error",
            return_value={"access_token": "secret-token-xyz"},
        ) as mock_silent,
    ):
        token = await auth.get_access_token()

        assert token == "secret-token-xyz"
        mock_silent.assert_called_once_with(
            scopes=DEFAULT_SCOPES,
            account=mock_account,
            force_refresh=False,
        )


async def test_get_access_token_no_account_raises(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)

    with (
        patch.object(auth._app, "get_accounts", return_value=[]),
        pytest.raises(AuthenticationRequiredError, match="No active Microsoft account session"),
    ):
        await auth.get_access_token()


async def test_get_access_token_silent_failure_raises(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)
    mock_account = {"username": "user@example.com"}

    with (
        patch.object(auth._app, "get_accounts", return_value=[mock_account]),
        patch.object(
            auth._app,
            "acquire_token_silent_with_error",
            return_value={"error": "invalid_grant", "error_description": "Token expired"},
        ),
        pytest.raises(AuthenticationRequiredError, match="Session expired"),
    ):
        await auth.get_access_token()


async def test_acquire_token_interactive_success(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)

    with patch.object(
        auth._app,
        "acquire_token_interactive",
        return_value={"access_token": "interactive-token", "id_token_claims": {}},
    ):
        result = await auth.acquire_token_interactive()
        assert result["access_token"] == "interactive-token"


async def test_acquire_token_interactive_consent_denied(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)

    with (
        patch.object(
            auth._app,
            "acquire_token_interactive",
            return_value={"error": "access_denied", "error_description": "User cancelled"},
        ),
        pytest.raises(ProviderPermissionError, match="Consent was denied"),
    ):
        await auth.acquire_token_interactive()


async def test_acquire_token_interactive_generic_error(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)

    with (
        patch.object(
            auth._app,
            "acquire_token_interactive",
            return_value={"error": "server_error", "error_description": "OAuth failure"},
        ),
        pytest.raises(AuthenticationRequiredError, match="Login failed"),
    ):
        await auth.acquire_token_interactive()


async def test_get_account_identity_from_cache(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)
    mock_account = {
        "username": "taylor@example.com",
        "home_account_id": "home-123",
        "name": "Taylor Smith",
        "realm": "tenant-abc",
    }

    with patch.object(auth._app, "get_accounts", return_value=[mock_account]):
        identity = await auth.get_account_identity_from_cache()

        assert identity is not None
        assert identity.provider is ProviderKind.MICROSOFT
        assert identity.provider_account_id == "home-123"
        assert identity.email_address == "taylor@example.com"
        assert identity.display_name == "Taylor Smith"
        assert identity.tenant_id == "tenant-abc"


async def test_get_account_identity_empty_cache(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)

    with patch.object(auth._app, "get_accounts", return_value=[]):
        identity = await auth.get_account_identity_from_cache()
        assert identity is None


async def test_disconnect_removes_accounts(
    memory_cache: msal.SerializableTokenCache,
) -> None:
    auth = MicrosoftAuth("client-id-123", token_cache=memory_cache)
    mock_account = {"username": "taylor@example.com"}

    with (
        patch.object(auth._app, "get_accounts", return_value=[mock_account]),
        patch.object(auth._app, "remove_account") as mock_remove,
    ):
        await auth.disconnect()
        mock_remove.assert_called_once_with(mock_account)
