"""Microsoft public-client authentication adapter."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import msal
from msal_extensions import (
    FilePersistence,
    PersistedTokenCache,
    build_encrypted_persistence,
)

from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
)

DEFAULT_SCOPES = ["User.Read", "Mail.Read"]
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"


def get_default_token_cache(
    cache_path: Path,
    *,
    allow_unencrypted_fallback: bool = False,
) -> msal.SerializableTokenCache:
    """Instantiate an encrypted persistent token cache or test fallback."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    location = str(cache_path.resolve())

    if allow_unencrypted_fallback:
        return PersistedTokenCache(FilePersistence(location))

    try:
        persistence = build_encrypted_persistence(location)
        return PersistedTokenCache(persistence)
    except Exception as exc:
        raise ProviderError(f"Failed to initialize secure token persistence: {exc}") from exc


class MicrosoftAuth:
    """Manages Microsoft public-client OAuth tokens and account sessions."""

    def __init__(
        self,
        client_id: str,
        *,
        token_cache: msal.SerializableTokenCache,
        authority: str = DEFAULT_AUTHORITY,
        scopes: Sequence[str] = DEFAULT_SCOPES,
    ) -> None:
        self._client_id = client_id
        self._authority = authority
        self._scopes = list(scopes)
        self._token_cache = token_cache
        self._lock = asyncio.Lock()
        self._app = msal.PublicClientApplication(
            client_id=self._client_id,
            authority=self._authority,
            token_cache=self._token_cache,
        )

    def _get_active_account(self) -> dict[str, Any] | None:
        accounts = self._app.get_accounts()
        return cast(dict[str, Any], accounts[0]) if accounts else None

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Acquire a valid access token silently, refreshing if needed."""
        async with self._lock:
            account = await asyncio.to_thread(self._get_active_account)
            if not account:
                raise AuthenticationRequiredError("No active Microsoft account session.")

            result = await asyncio.to_thread(
                self._app.acquire_token_silent_with_error,
                scopes=self._scopes,
                account=account,
                force_refresh=force_refresh,
            )

            if not result or "access_token" not in result:
                error = result.get("error") if result else "unknown_error"
                description = result.get("error_description", "Silent token acquisition failed.")
                if error in {"invalid_grant", "interaction_required"}:
                    raise AuthenticationRequiredError(f"Session expired: {description}")
                raise AuthenticationRequiredError(f"Authentication failed: {description}")

            return str(result["access_token"])

    async def acquire_token_interactive(self) -> dict[str, Any]:
        """Launch the system browser to authenticate interactively."""
        async with self._lock:
            result = await asyncio.to_thread(
                self._app.acquire_token_interactive,
                scopes=self._scopes,
                prompt="select_account",
            )

            if "access_token" not in result:
                error = result.get("error", "unknown_error")
                description = result.get("error_description", "Interactive login failed.")
                if error == "access_denied":
                    raise ProviderPermissionError(f"Consent was denied: {description}")
                raise AuthenticationRequiredError(f"Login failed ({error}): {description}")

            return cast(dict[str, Any], result)

    async def get_account_identity_from_cache(self) -> AccountIdentity | None:
        """Return the current cached account identity without network calls."""
        account = await asyncio.to_thread(self._get_active_account)
        if not account:
            return None

        username = account.get("username")
        if not username or "@" not in username:
            return None

        return AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id=str(account.get("home_account_id") or username),
            email_address=username,
            display_name=account.get("name"),
            tenant_id=account.get("realm"),
        )

    async def disconnect(self) -> None:
        """Remove all accounts from cache and flush persistence."""
        async with self._lock:
            accounts = await asyncio.to_thread(self._app.get_accounts)
            for account in accounts:
                await asyncio.to_thread(self._app.remove_account, account)
