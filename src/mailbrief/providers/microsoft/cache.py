"""Encrypted Microsoft token-cache construction and recovery operations."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import msal
from msal_extensions import FilePersistence, PersistedTokenCache, build_encrypted_persistence

from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import ProviderError


def get_default_token_cache(
    cache_path: Path,
    *,
    allow_unencrypted_fallback: bool = False,
) -> msal.SerializableTokenCache:
    """Create an encrypted persistent token cache, or an explicit test cache."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        location = str(cache_path.resolve())
        persistence = (
            FilePersistence(location)
            if allow_unencrypted_fallback
            else build_encrypted_persistence(location)
        )
        return PersistedTokenCache(persistence)
    except Exception as exc:
        raise ConfigurationError("Unable to initialize secure Microsoft credentials.") from exc


def _credential_types() -> tuple[str, ...]:
    credential_type = msal.TokenCache.CredentialType
    return (
        credential_type.ACCOUNT,
        credential_type.ACCESS_TOKEN,
        credential_type.REFRESH_TOKEN,
        credential_type.ID_TOKEN,
    )


def _credential_snapshots(
    cache: msal.SerializableTokenCache,
    credential_types: tuple[str, ...],
) -> list[tuple[str, dict[str, Any]]]:
    # PersistedTokenCache reloads before search. Serialization then includes access
    # tokens with ext_cache_key, which an unqualified MSAL search intentionally omits.
    list(cache.search(msal.TokenCache.CredentialType.ACCOUNT))
    payload = json.loads(cache.serialize())
    if not isinstance(payload, Mapping):
        raise ValueError("invalid cache root")

    snapshots: list[tuple[str, dict[str, Any]]] = []
    for credential_type in credential_types:
        entries = payload.get(credential_type, {})
        if not isinstance(entries, Mapping):
            raise ValueError("invalid credential collection")
        for entry in entries.values():
            if not isinstance(entry, dict):
                raise ValueError("invalid credential entry")
            snapshots.append((credential_type, entry))
    return snapshots


def _clear_cache(cache_path: Path) -> None:
    cache = get_default_token_cache(cache_path)
    credential_types = _credential_types()
    try:
        for credential_type, entry in _credential_snapshots(cache, credential_types):
            cache.modify(credential_type, entry)

        if _credential_snapshots(cache, credential_types):
            raise ProviderError("Microsoft credentials could not be completely removed.")
    except ProviderError:
        raise
    except Exception as exc:
        raise ConfigurationError("Unable to remove secure Microsoft credentials.") from exc


async def clear_microsoft_session(cache_path: Path) -> None:
    """Remove Microsoft credentials without constructing MSAL or using the network."""
    await asyncio.to_thread(_clear_cache, cache_path)
