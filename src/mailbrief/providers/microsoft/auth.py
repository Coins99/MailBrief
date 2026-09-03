"""Microsoft public-client authentication adapter."""

import asyncio
import base64
import binascii
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any, cast

import msal

from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.ports.errors import (
    AuthenticationCancelledError,
    AuthenticationRequiredError,
    ProviderError,
)
from mailbrief.providers.microsoft.cache import get_default_token_cache

DEFAULT_SCOPES = ["User.Read", "Mail.Read"]
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"

_AUTHENTICATION_REQUIRED_CODES = {
    "consent_required",
    "interaction_required",
    "invalid_grant",
}


async def _run_thread_to_completion[T](operation: Callable[[], T]) -> T:
    """Run blocking work without abandoning its worker when the caller is cancelled."""
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        with suppress(BaseException):
            worker.result()
        raise cancellation


class MicrosoftAuth:
    """Manage Microsoft public-client OAuth tokens and one selected account."""

    def __init__(
        self,
        client_id: str,
        *,
        token_cache: msal.SerializableTokenCache,
        authority: str = DEFAULT_AUTHORITY,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        _application: msal.PublicClientApplication | None = None,
    ) -> None:
        self._client_id = client_id
        self._authority = authority
        self._scopes = list(scopes)
        self._token_cache = token_cache
        self._lock = asyncio.Lock()
        self._active_account: dict[str, Any] | None = None
        self._app = _application or msal.PublicClientApplication(
            client_id=self._client_id,
            authority=self._authority,
            token_cache=self._token_cache,
        )

    @classmethod
    async def create(
        cls,
        client_id: str,
        *,
        token_cache: msal.SerializableTokenCache,
        authority: str = DEFAULT_AUTHORITY,
        scopes: Sequence[str] = DEFAULT_SCOPES,
    ) -> "MicrosoftAuth":
        """Construct MSAL away from the event-loop/UI thread."""
        scope_list = list(scopes)
        try:
            application = await _run_thread_to_completion(
                lambda: msal.PublicClientApplication(
                    client_id=client_id,
                    authority=authority,
                    token_cache=token_cache,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ProviderError("Unable to initialize Microsoft authentication.") from exc
        return cls(
            client_id,
            token_cache=token_cache,
            authority=authority,
            scopes=scope_list,
            _application=application,
        )

    async def _run_blocking_exclusive[T](self, operation: Callable[[], T]) -> T:
        async with self._lock:
            return await _run_thread_to_completion(operation)

    @staticmethod
    def _account_home_id(account: Mapping[str, Any]) -> str | None:
        value = account.get("home_account_id")
        return value if isinstance(value, str) and value else None

    def _list_accounts(self) -> list[dict[str, Any]]:
        return [cast(dict[str, Any], account) for account in self._app.get_accounts()]

    def _resolve_active_account(self) -> dict[str, Any] | None:
        accounts = self._list_accounts()
        if self._active_account is not None:
            active_home_id = self._account_home_id(self._active_account)
            matches = [
                account
                for account in accounts
                if active_home_id is not None and self._account_home_id(account) == active_home_id
            ]
            if len(matches) == 1:
                self._active_account = matches[0]
                return matches[0]
            self._active_account = None
            return None

        if len(accounts) != 1 or self._account_home_id(accounts[0]) is None:
            return None
        self._active_account = accounts[0]
        return accounts[0]

    @staticmethod
    def _home_account_id_from_result(result: Mapping[str, Any]) -> str | None:
        client_info = result.get("client_info")
        if not isinstance(client_info, str) or not client_info or len(client_info) > 16_384:
            return None
        try:
            padding = "=" * (-len(client_info) % 4)
            decoded = base64.urlsafe_b64decode(client_info + padding).decode("utf-8")
            payload = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        uid = payload.get("uid")
        utid = payload.get("utid")
        if not isinstance(uid, str) or not uid or not isinstance(utid, str) or not utid:
            return None
        return f"{uid}.{utid}"

    def _select_interactive_account(self, result: Mapping[str, Any]) -> dict[str, Any]:
        accounts = self._list_accounts()
        result_home_id = self._home_account_id_from_result(result)
        matches = [
            account
            for account in accounts
            if result_home_id is not None and self._account_home_id(account) == result_home_id
        ]

        if len(matches) != 1:
            claims = result.get("id_token_claims")
            username: str | None = None
            if isinstance(claims, Mapping):
                candidate = claims.get("preferred_username") or claims.get("upn")
                if isinstance(candidate, str) and candidate:
                    username = candidate.casefold()
            matches = (
                [
                    account
                    for account in accounts
                    if isinstance(account.get("username"), str)
                    and cast(str, account["username"]).casefold() == username
                ]
                if username is not None
                else []
            )

        if len(matches) != 1 or self._account_home_id(matches[0]) is None:
            raise AuthenticationRequiredError("Unable to identify the selected Microsoft account.")

        selected = matches[0]
        self._active_account = selected
        self._remove_other_accounts(selected)
        return selected

    def _remove_other_accounts(self, selected: Mapping[str, Any]) -> None:
        selected_home_id = self._account_home_id(selected)
        for account in self._list_accounts():
            if self._account_home_id(account) != selected_home_id:
                self._app.remove_account(account)

    @staticmethod
    def _error_code(result: Mapping[str, Any]) -> str | None:
        error = result.get("error")
        return error if isinstance(error, str) else None

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Acquire a valid access token silently, refreshing if needed."""

        def acquire() -> str:
            account = self._resolve_active_account()
            if account is None:
                raise AuthenticationRequiredError("No unambiguous Microsoft account session.")
            result = self._app.acquire_token_silent_with_error(
                scopes=self._scopes,
                account=account,
                force_refresh=force_refresh,
            )
            if result is None:
                raise AuthenticationRequiredError("Microsoft authentication is required.")
            if isinstance(result, Mapping):
                access_token = result.get("access_token")
                if isinstance(access_token, str) and access_token:
                    return access_token
                if self._error_code(result) in _AUTHENTICATION_REQUIRED_CODES:
                    raise AuthenticationRequiredError("Microsoft authentication is required.")
            raise ProviderError("Microsoft authentication service failed.")

        try:
            return await self._run_blocking_exclusive(acquire)
        except (AuthenticationRequiredError, ProviderError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise ProviderError("Microsoft authentication service failed.") from exc

    async def acquire_token_interactive(self) -> None:
        """Launch the system browser and retain the uniquely selected account."""

        def acquire() -> None:
            result = self._app.acquire_token_interactive(
                scopes=self._scopes,
                prompt="select_account",
            )
            if result is None:
                raise AuthenticationCancelledError("Microsoft sign-in was cancelled.")
            if not isinstance(result, Mapping):
                raise ProviderError("Microsoft authentication service failed.")

            access_token = result.get("access_token")
            if isinstance(access_token, str) and access_token:
                self._select_interactive_account(result)
                return

            error = self._error_code(result)
            if error == "access_denied":
                raise AuthenticationCancelledError("Microsoft sign-in was cancelled.")
            if error in _AUTHENTICATION_REQUIRED_CODES:
                raise AuthenticationRequiredError("Microsoft authentication is required.")
            raise ProviderError("Microsoft authentication service failed.")

        try:
            await self._run_blocking_exclusive(acquire)
        except (AuthenticationRequiredError, ProviderError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise ProviderError("Microsoft authentication service failed.") from exc

    async def get_account_identity_from_cache(self) -> AccountIdentity | None:
        """Return the selected cached identity without network calls."""

        def resolve() -> AccountIdentity | None:
            account = self._resolve_active_account()
            if account is None:
                return None
            home_id = self._account_home_id(account)
            username = account.get("username")
            name = account.get("name")
            if (
                home_id is None
                or not isinstance(username, str)
                or not isinstance(name, (str, type(None)))
            ):
                return None
            tenant_id = home_id.split(".", maxsplit=1)[1] if "." in home_id else None
            try:
                return AccountIdentity(
                    provider=ProviderKind.MICROSOFT,
                    provider_account_id=home_id,
                    email_address=username,
                    display_name=name,
                    tenant_id=tenant_id,
                )
            except ValueError:
                return None

        try:
            return await self._run_blocking_exclusive(resolve)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ProviderError("Unable to read the Microsoft account session.") from exc

    async def disconnect(self) -> None:
        """Remove all PCA accounts and verify no account records remain."""

        def remove() -> None:
            for account in self._list_accounts():
                self._app.remove_account(account)
            self._active_account = None
            if self._list_accounts():
                raise ProviderError("Microsoft credentials could not be completely removed.")

        try:
            await self._run_blocking_exclusive(remove)
        except (ProviderError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise ProviderError("Unable to remove Microsoft credentials.") from exc


__all__ = [
    "DEFAULT_AUTHORITY",
    "DEFAULT_SCOPES",
    "MicrosoftAuth",
    "get_default_token_cache",
]
