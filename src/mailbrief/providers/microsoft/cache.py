"""Encrypted Microsoft token-cache construction and recovery operations."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import msal
from msal_extensions import (
    CrossPlatLock,
    FilePersistence,
    PersistedTokenCache,
    build_encrypted_persistence,
)

from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import ProviderError


class ManagedTokenCache(PersistedTokenCache):
    """Encrypted persistent token cache with an explicit sticky purge lifecycle."""

    def __init__(self, persistence: Any) -> None:
        super().__init__(persistence)
        self.is_purged: bool = False

    def modify(
        self,
        credential_type: Any,
        old_entry: Any,
        new_key_value_pairs: Any = None,
    ) -> None:
        if self.is_purged:
            return
        super().modify(credential_type, old_entry, new_key_value_pairs=new_key_value_pairs)

    def purge_credentials(self) -> None:
        """Clear all credentials from persistence and memory, permanently locking against writes."""
        if hasattr(self, "_persistence") and hasattr(self, "_lock_location"):
            try:
                with CrossPlatLock(self._lock_location):
                    self._persistence.save("{}")
            except Exception:
                pass
        self.is_purged = True
        self.deserialize("{}")
        self.has_state_changed = False


def get_default_token_cache(
    cache_path: Path,
    *,
    allow_unencrypted_fallback: bool = False,
) -> ManagedTokenCache:
    """Create an encrypted persistent token cache, or an explicit test cache."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        location = str(cache_path.resolve())
        persistence = (
            FilePersistence(location)
            if allow_unencrypted_fallback
            else build_encrypted_persistence(location)
        )
        return ManagedTokenCache(persistence)
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


def purge_token_cache(
    cache_path: Path,
    token_cache: msal.SerializableTokenCache | None = None,
) -> None:
    """Purge token cache from disk and memory, permanently locking it against writes."""
    if token_cache is not None:
        if hasattr(token_cache, "purge_credentials"):
            token_cache.purge_credentials()
        else:
            token_cache.deserialize("{}")
            token_cache.has_state_changed = False
            setattr(token_cache, "is_purged", True)

        # Assert zero tokens remaining in memory as part of the postcondition
        access_tokens = token_cache.find(msal.TokenCache.CredentialType.ACCESS_TOKEN)
        accounts = token_cache.find(msal.TokenCache.CredentialType.ACCOUNT)
        refresh_tokens = token_cache.find(msal.TokenCache.CredentialType.REFRESH_TOKEN)
        id_tokens = token_cache.find(msal.TokenCache.CredentialType.ID_TOKEN)
        if (
            len(access_tokens) != 0
            or len(accounts) != 0
            or len(refresh_tokens) != 0
            or len(id_tokens) != 0
        ):
            raise ProviderError("Failed to clear in-memory credentials during cache purge.")

    try:
        if cache_path.exists():
            cache_path.unlink()
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        raise ProviderError(
            "Unable to remove Microsoft credentials file (locked by another process)."
        ) from exc
    except OSError as exc:
        raise ConfigurationError("Unable to remove secure Microsoft credentials.") from exc

    if cache_path.exists():
        raise ProviderError("Microsoft credentials could not be completely removed from disk.")


def _clear_cache(cache_path: Path) -> None:
    cache = get_default_token_cache(cache_path)
    if hasattr(cache, "_persistence"):
        print(f"Purging persistence layer: {type(cache._persistence).__name__}")
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

    try:
        if cache_path.exists():
            cache_path.unlink()
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        raise ProviderError(
            "Unable to remove Microsoft credentials file (locked by another process)."
        ) from exc
    except OSError as exc:
        raise ConfigurationError("Unable to remove secure Microsoft credentials.") from exc

    if cache_path.exists():
        raise ProviderError("Microsoft credentials could not be completely removed from disk.")


async def clear_microsoft_session(cache_path: Path) -> None:
    """Remove Microsoft credentials without constructing MSAL or using the network."""
    await asyncio.to_thread(_clear_cache, cache_path)

