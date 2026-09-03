"""Tests for encrypted Microsoft token-cache lifecycle operations."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import msal
import pytest
from msal_extensions import PersistedTokenCache

from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import ProviderError
from mailbrief.providers.microsoft.cache import (
    _credential_types,
    clear_microsoft_session,
    get_default_token_cache,
)


def test_unencrypted_cache_is_available_only_when_explicit(tmp_path: Path) -> None:
    cache = get_default_token_cache(
        tmp_path / "tokens.bin",
        allow_unencrypted_fallback=True,
    )
    assert isinstance(cache, PersistedTokenCache)


def test_secure_cache_initialization_is_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "private-location" / "tokens.bin"
    with (
        patch(
            "mailbrief.providers.microsoft.cache.build_encrypted_persistence",
            side_effect=OSError("private failure and path"),
        ),
        pytest.raises(ConfigurationError) as exc_info,
    ):
        get_default_token_cache(path)
    assert str(path) not in str(exc_info.value)
    assert "private failure" not in str(exc_info.value)


async def test_clear_session_removes_all_tokens_including_extended_access_tokens(
    tmp_path: Path,
) -> None:
    cache = msal.SerializableTokenCache()
    base_entry = {
        "home_account_id": "uid.tenant",
        "environment": "login.microsoftonline.com",
        "client_id": "client-id",
        "realm": "tenant",
        "target": "Mail.Read User.Read",
        "authority_type": "MSSTS",
        "expires_on": "4102444800",
    }
    for credential_type in _credential_types():
        entry = {**base_entry, "credential_type": credential_type}
        cache.modify(credential_type, entry, entry)

    extended_entry = {
        **base_entry,
        "credential_type": msal.TokenCache.CredentialType.ACCESS_TOKEN,
        "ext_cache_key": "extended-key",
    }
    cache.modify(
        msal.TokenCache.CredentialType.ACCESS_TOKEN,
        extended_entry,
        extended_entry,
    )
    app_metadata = {"environment": "login.microsoftonline.com", "client_id": "client-id"}
    cache.modify(msal.TokenCache.CredentialType.APP_METADATA, app_metadata, app_metadata)

    with patch("mailbrief.providers.microsoft.cache.get_default_token_cache", return_value=cache):
        await clear_microsoft_session(tmp_path / "tokens.bin")

    serialized = json.loads(cache.serialize())
    assert all(not serialized.get(credential_type) for credential_type in _credential_types())
    assert serialized[msal.TokenCache.CredentialType.APP_METADATA]


async def test_clear_empty_session_succeeds(tmp_path: Path) -> None:
    cache = msal.SerializableTokenCache()
    with patch("mailbrief.providers.microsoft.cache.get_default_token_cache", return_value=cache):
        await clear_microsoft_session(tmp_path / "missing.bin")
    assert not json.loads(cache.serialize())


async def test_clear_corrupt_session_is_sanitized(tmp_path: Path) -> None:
    cache = MagicMock(spec=msal.SerializableTokenCache)
    cache.search.side_effect = ValueError("raw corrupt cache bytes")
    with (
        patch("mailbrief.providers.microsoft.cache.get_default_token_cache", return_value=cache),
        pytest.raises(ConfigurationError) as exc_info,
    ):
        await clear_microsoft_session(tmp_path / "tokens.bin")
    assert "raw corrupt" not in str(exc_info.value)


@pytest.mark.parametrize(
    "serialized",
    [
        "[]",
        json.dumps({msal.TokenCache.CredentialType.ACCOUNT: []}),
        json.dumps({msal.TokenCache.CredentialType.ACCOUNT: {"key": []}}),
    ],
)
async def test_clear_rejects_malformed_serialized_schema(
    tmp_path: Path,
    serialized: str,
) -> None:
    cache = MagicMock(spec=msal.SerializableTokenCache)
    cache.search.return_value = []
    cache.serialize.return_value = serialized
    with (
        patch("mailbrief.providers.microsoft.cache.get_default_token_cache", return_value=cache),
        pytest.raises(ConfigurationError),
    ):
        await clear_microsoft_session(tmp_path / "tokens.bin")


async def test_clear_reports_credentials_that_remain_after_removal(tmp_path: Path) -> None:
    cache = MagicMock(spec=msal.SerializableTokenCache)
    cache.search.return_value = []
    account_collection = {
        msal.TokenCache.CredentialType.ACCOUNT: {"key": {"home_account_id": "uid.tenant"}}
    }
    cache.serialize.return_value = json.dumps(account_collection)
    with (
        patch("mailbrief.providers.microsoft.cache.get_default_token_cache", return_value=cache),
        pytest.raises(ProviderError, match="completely removed"),
    ):
        await clear_microsoft_session(tmp_path / "tokens.bin")
