"""Direct asynchronous Microsoft Graph client with safe token injection and retries."""

import asyncio
import email.utils
import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit

import httpx

from mailbrief import __version__
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft.auth import MicrosoftAuth

MAX_RETRIES = 3
MAX_CUMULATIVE_DELAY_SECONDS = 60.0
TRANSIENT_SERVER_DELAYS = (1.0, 2.0, 4.0)
DEFAULT_TIMEOUT = httpx.Timeout(timeout=30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)

_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RESERVED_HEADERS = {
    "authorization",
    "client-request-id",
    "return-client-request-id",
    "user-agent",
}


def parse_retry_after(header_value: str | None) -> float | None:
    """Parse nonnegative delta-seconds or an HTTP date."""
    if not header_value:
        return None

    try:
        seconds = float(header_value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass

    try:
        target_dt = email.utils.parsedate_to_datetime(header_value)
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=UTC)
        return max(0.0, (target_dt - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class GraphClient:
    """HTTP client for Microsoft Graph with bounded, sanitized failure handling."""

    def __init__(
        self,
        auth: MicrosoftAuth,
        *,
        base_url: str = "https://graph.microsoft.com/v1.0",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = self._parse_base_url(base_url)
        self._scheme = parsed.scheme.lower()
        self._hostname = (parsed.hostname or "").lower()
        self._port = parsed.port or 443
        self._base_path = parsed.path.rstrip("/")
        self._base_url = parsed.geturl().rstrip("/")
        self._auth = auth
        self._client = http_client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
        )
        self._owns_client = http_client is None

    @classmethod
    def _parse_base_url(cls, base_url: str) -> SplitResult:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise ProviderResponseError("Invalid Microsoft Graph base URL.") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
            or not parsed.path.rstrip("/")
        ):
            raise ProviderResponseError("Invalid Microsoft Graph base URL.")
        cls._validate_path(
            parsed.path,
            parsed.path.rstrip("/"),
            reject_encoded_separators=True,
        )
        return parsed

    @staticmethod
    def _validate_path(
        path: str,
        base_path: str,
        *,
        reject_encoded_separators: bool,
    ) -> None:
        decoded = path
        for _ in range(3):
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if (
            "\\" in decoded
            or (reject_encoded_separators and decoded.count("/") != path.count("/"))
            or any(segment in {".", ".."} for segment in decoded.split("/"))
            or not (decoded == base_path or decoded.startswith(f"{base_path}/"))
        ):
            raise ProviderResponseError("Untrusted Microsoft Graph request URL.")

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client only when this adapter owns it."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    def _validate_url(self, url: str) -> tuple[str, bool]:
        """Validate a relative endpoint or same-API-root continuation URL."""
        try:
            candidate = urlsplit(url)
            candidate_port = candidate.port
        except ValueError as exc:
            raise ProviderResponseError("Untrusted Microsoft Graph request URL.") from exc

        is_absolute = bool(candidate.scheme or candidate.netloc)
        if is_absolute:
            if (
                candidate.scheme.lower() != self._scheme
                or (candidate.hostname or "").lower() != self._hostname
                or (candidate_port or 443) != self._port
                or candidate.username is not None
                or candidate.password is not None
                or candidate.fragment
            ):
                raise ProviderResponseError("Untrusted Microsoft Graph request URL.")
            self._validate_path(
                candidate.path,
                self._base_path,
                reject_encoded_separators=True,
            )
            return url, True

        if candidate.fragment or not candidate.path:
            raise ProviderResponseError("Untrusted Microsoft Graph request URL.")
        target = f"{self._base_url}/{url.lstrip('/')}"
        target_parts = urlsplit(target)
        self._validate_path(
            target_parts.path,
            self._base_path,
            reject_encoded_separators=False,
        )
        return target, False

    @staticmethod
    def _validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
        caller_headers = dict(headers or {})
        if any(name.casefold() in _RESERVED_HEADERS for name in caller_headers):
            raise ProviderResponseError("A reserved Microsoft Graph header was supplied.")
        return caller_headers

    async def get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a GET request with bearer auth and bounded retry behavior."""
        target_url, is_continuation = self._validate_url(path_or_url)
        caller_headers = self._validate_headers(headers)
        request_params = None if is_continuation else params

        token = await self._auth.get_access_token()
        refreshed_401 = False
        retry_count = 0
        cumulative_delay = 0.0

        while True:
            request_headers = {
                **caller_headers,
                "Authorization": f"Bearer {token}",
                "client-request-id": str(uuid.uuid4()),
                "return-client-request-id": "true",
                "User-Agent": f"MailBrief/{__version__}",
            }
            try:
                response = await self._client.get(
                    target_url,
                    params=request_params,
                    headers=request_headers,
                )
            except httpx.TransportError as exc:
                if retry_count < MAX_RETRIES:
                    delay = TRANSIENT_SERVER_DELAYS[retry_count]
                    retry_count += 1
                    cumulative_delay += delay
                    await asyncio.sleep(delay)
                    continue
                raise ProviderError("Microsoft Graph network request failed.") from exc

            if response.status_code == 401 and not refreshed_401:
                refreshed_401 = True
                token = await self._auth.get_access_token(force_refresh=True)
                continue
            if response.status_code == 401:
                raise AuthenticationRequiredError("Microsoft authentication is required.")

            if response.status_code == 429:
                retry_delay = parse_retry_after(response.headers.get("Retry-After"))
                if retry_delay is None:
                    retry_delay = (
                        TRANSIENT_SERVER_DELAYS[retry_count]
                        if retry_count < len(TRANSIENT_SERVER_DELAYS)
                        else TRANSIENT_SERVER_DELAYS[-1]
                    )
                if (
                    retry_count >= MAX_RETRIES
                    or cumulative_delay + retry_delay > MAX_CUMULATIVE_DELAY_SECONDS
                ):
                    raise ProviderRateLimitError(
                        "Microsoft Graph rate limit exceeded.",
                        retry_after_seconds=retry_delay,
                    )
                retry_count += 1
                cumulative_delay += retry_delay
                await asyncio.sleep(retry_delay)
                continue

            if response.status_code in {500, 502, 503, 504}:
                if retry_count < MAX_RETRIES:
                    delay = TRANSIENT_SERVER_DELAYS[retry_count]
                    if cumulative_delay + delay <= MAX_CUMULATIVE_DELAY_SECONDS:
                        retry_count += 1
                        cumulative_delay += delay
                        await asyncio.sleep(delay)
                        continue
                raise ProviderResponseError(
                    f"Microsoft Graph server error ({response.status_code})."
                )

            if response.status_code == 403:
                raise ProviderPermissionError(self._status_message(response, "permission denied"))

            if not 200 <= response.status_code < 300:
                raise ProviderResponseError(self._status_message(response, "request failed"))

            if response.status_code == 204:
                raise ProviderResponseError("Microsoft Graph returned an empty response.")
            try:
                result = response.json()
            except ValueError as exc:
                raise ProviderResponseError("Microsoft Graph returned invalid JSON.") from exc
            if not isinstance(result, dict):
                raise ProviderResponseError("Microsoft Graph response must be a JSON object.")
            return result

    @staticmethod
    def _extract_error_code(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, Mapping):
            return None
        error = payload.get("error")
        if not isinstance(error, Mapping):
            return None
        code = error.get("code")
        return code if isinstance(code, str) and _ERROR_CODE_PATTERN.fullmatch(code) else None

    @classmethod
    def _status_message(cls, response: httpx.Response, outcome: str) -> str:
        code = cls._extract_error_code(response)
        suffix = f" Code: {code}." if code else ""
        return f"Microsoft Graph {outcome} ({response.status_code}).{suffix}"
