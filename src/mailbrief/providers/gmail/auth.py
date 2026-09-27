"""Gmail read-only account authentication and secure session restoration."""

import asyncio
import time
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderPermissionError,
    ProviderRateLimitError,
)
from mailbrief.providers.gmail.cache import CredentialStore, RefreshCredential
from mailbrief.providers.gmail.errors import GmailSetupError, permission_guidance, response_error
from mailbrief.providers.gmail.oauth import (
    GMAIL_SCOPE,
    TOKEN_URL,
    AuthorizationGrant,
    DesktopClient,
)

PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class Authorizer(Protocol):
    """Injectable browser authorization boundary."""

    async def authorize(self, client: DesktopClient) -> AuthorizationGrant: ...


class TokenResponse(BaseModel):
    """Validate token responses without putting sensitive input in errors/reprs."""

    model_config = ConfigDict(hide_input_in_errors=True)

    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr | None = Field(default=None, min_length=1)
    expires_in: int = Field(gt=0, le=86_400, strict=True)
    token_type: str
    scope: str | None = None


class GmailAuth:
    """Single-account session; validate mailbox identity before saving credentials."""

    def __init__(
        self,
        client: DesktopClient,
        store: CredentialStore,
        http: httpx.AsyncClient,
        authorizer: Authorizer,
    ) -> None:
        self._client = client
        self._store = store
        self._http = http
        self._authorizer = authorizer
        self._token: SecretStr | None = None
        self._expires_at = 0.0
        self._account: AccountIdentity | None = None
        self._expected_email: str | None = None
        self._lock = asyncio.Lock()

    async def connect(self, *, silent_only: bool = False) -> AccountIdentity:
        """Restore first; only an explicitly interactive call may open a browser."""
        async with self._lock:
            return await self._connect(silent_only=silent_only)

    async def access_token(self) -> str:
        """Return a current token for the Gmail adapter, without opening a browser."""
        async with self._lock:
            await self._connect(silent_only=True)
            if self._token is None:
                raise AuthenticationRequiredError("Connect Gmail before fetching messages.")
            return self._token.get_secret_value()

    async def disconnect(self) -> None:
        """Forget local credentials; neither revoke Google access nor delete mail/data."""
        async with self._lock:
            self._token = None
            self._account = None
            self._expected_email = None
            self._expires_at = 0
            await asyncio.to_thread(self._store.clear)

    async def invalidate_access_token(self, rejected_token: str) -> None:
        """Invalidate only the rejected generation when concurrent requests see a 401."""
        async with self._lock:
            if self._token is not None and self._token.get_secret_value() == rejected_token:
                self._expires_at = 0

    async def _connect(self, *, silent_only: bool) -> AccountIdentity:
        if self._account is not None and self._token and time.monotonic() < self._expires_at:
            return self._account
        self._token = None
        self._account = None
        cached = await asyncio.to_thread(self._store.load)
        if (
            cached is not None
            and self._expected_email is not None
            and cached.email_address.casefold() != self._expected_email
        ):
            raise AuthenticationRequiredError(
                "The saved Gmail account changed; disconnect and restart synchronization."
            )
        if cached is not None and cached.client_id != self._client.client_id:
            raise GmailSetupError("Gmail OAuth client changed; disconnect before reconnecting.")
        tokens: TokenResponse | None = None
        restored = False
        if cached is not None:
            try:
                tokens = await self._exchange(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": cached.refresh_token.get_secret_value(),
                    }
                )
                restored = True
            except AuthenticationRequiredError:
                if silent_only:
                    raise
        if tokens is None:
            if silent_only:
                raise AuthenticationRequiredError(
                    "No restorable Gmail session; connect interactively."
                )
            grant = await self._authorizer.authorize(self._client)
            tokens = await self._exchange(
                {
                    "grant_type": "authorization_code",
                    "code": grant.code,
                    "code_verifier": grant.verifier,
                    "redirect_uri": grant.redirect_uri,
                }
            )
        account = await self._profile(tokens.access_token)
        if (
            self._expected_email is not None
            and account.email_address.casefold() != self._expected_email
        ):
            raise AuthenticationRequiredError(
                "The Gmail session account changed; disconnect before switching accounts."
            )
        if (
            cached is not None
            and account.email_address.casefold() != cached.email_address.casefold()
        ):
            raise AuthenticationRequiredError(
                "Google returned a different mailbox; disconnect before switching accounts."
            )
        refresh = tokens.refresh_token or (cached.refresh_token if restored and cached else None)
        if refresh is None:
            raise AuthenticationRequiredError("Google did not grant offline access; reconnect.")
        credential = RefreshCredential(
            client_id=self._client.client_id,
            email_address=account.email_address,
            refresh_token=refresh,
        )
        await asyncio.to_thread(self._store.save, credential)
        self._token = tokens.access_token
        self._expires_at = time.monotonic() + max(0, tokens.expires_in - 60)
        self._account = account
        self._expected_email = account.email_address.casefold()
        return account

    async def _exchange(self, fields: dict[str, str]) -> TokenResponse:
        fields = {
            **fields,
            "client_id": self._client.client_id,
            "client_secret": self._client.client_secret,
        }
        try:
            response = await self._http.post(TOKEN_URL, data=fields, follow_redirects=False)
        except httpx.HTTPError:
            raise response_error("Unable to contact Google authentication; retry later.") from None
        if response.status_code in {400, 401}:
            try:
                error = response.json()
            except ValueError:
                error = None
            if isinstance(error, dict) and error.get("error") == "invalid_grant":
                raise AuthenticationRequiredError(
                    "Gmail authorization expired or was revoked; reconnect."
                )
            reason = error.get("error") if isinstance(error, dict) else None
            if reason in ("invalid_client", "deleted_client"):
                raise GmailSetupError(
                    "Google rejected the OAuth client. Download the current Desktop app "
                    "client JSON from Google Cloud and check MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH."
                )
            if reason == "unauthorized_client":
                raise GmailSetupError(
                    "Google does not authorize this OAuth client for desktop sign-in. "
                    "Check that the client type is Desktop app."
                )
        stage: Literal["token refresh", "token exchange"] = (
            "token refresh" if fields["grant_type"] == "refresh_token" else "token exchange"
        )
        self._check_status(response, stage=stage)
        try:
            tokens = TokenResponse.model_validate_json(response.content)
        except ValidationError:
            raise response_error("Google returned an invalid token response.") from None
        if tokens.token_type.casefold() != "bearer":
            raise response_error("Google returned an unsupported token type.")
        if tokens.scope is not None and GMAIL_SCOPE not in tokens.scope.split():
            raise ProviderPermissionError("Gmail read-only access was not granted.")
        return tokens

    async def _profile(self, token: SecretStr) -> AccountIdentity:
        try:
            response = await self._http.get(
                PROFILE_URL,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise response_error("Unable to verify the Gmail account; retry later.") from None
        self._check_status(response, stage="Gmail profile check")
        try:
            email = response.json()["emailAddress"]
            return AccountIdentity(
                provider=ProviderKind.GMAIL,
                provider_account_id=email,
                email_address=email,
            )
        except (ValueError, KeyError, TypeError):
            raise response_error("Google returned an invalid Gmail identity.") from None

    @staticmethod
    def _check_status(
        response: httpx.Response,
        *,
        stage: Literal["token refresh", "token exchange", "Gmail profile check"] = "token exchange",
    ) -> None:
        if response.status_code == 401:
            raise AuthenticationRequiredError("Gmail authorization was rejected; reconnect.")
        if response.status_code == 403:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise ProviderPermissionError(permission_guidance(payload))
        if response.status_code == 429:
            raise ProviderRateLimitError("Google rate limit reached; retry later.")
        if response.status_code != 200:
            guidance = (
                "Google is temporarily unavailable; retry shortly."
                if response.status_code >= 500
                else "Check OAuth and Gmail setup; report this status and stage if it persists."
            )
            raise response_error(f"Google {stage} failed (HTTP {response.status_code}). {guidance}")
