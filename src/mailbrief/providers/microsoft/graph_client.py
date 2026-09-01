"""Direct asynchronous Microsoft Graph client with token injection and backoff."""

import asyncio
import email.utils
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft.auth import MicrosoftAuth

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
MAX_CUMULATIVE_DELAY_SECONDS = 60.0
TRANSIENT_SERVER_DELAYS = (1.0, 2.0, 4.0)
DEFAULT_TIMEOUT = httpx.Timeout(timeout=30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)


def parse_retry_after(header_value: str | None) -> float | None:
    """Parse integer seconds or RFC 7231 / RFC 2822 HTTP date from Retry-After."""
    if not header_value:
        return None

    # Try delta-seconds integer/float
    try:
        seconds = float(header_value)
        return max(0.0, seconds)
    except ValueError:
        pass

    # Try HTTP date
    try:
        target_dt = email.utils.parsedate_to_datetime(header_value)
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=UTC)
        diff = (target_dt - datetime.now(UTC)).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


class GraphClient:
    """HTTP client for Microsoft Graph with rate-limit, timeout, and retry handling."""

    def __init__(
        self,
        auth: MicrosoftAuth,
        *,
        base_url: str = "https://graph.microsoft.com/v1.0",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._expected_host = urlparse(self._base_url).netloc
        self._client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._owns_client = http_client is None

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owns_client:
            await self._client.aclose()

    def _validate_url(self, url: str) -> str:
        """Validate URL to prevent SSRF when following pagination links."""
        parsed = urlparse(url)
        if not parsed.netloc:
            # Relative path: prefix with base_url
            return f"{self._base_url}/{url.lstrip('/')}"

        if parsed.scheme != "https" or parsed.netloc.lower() != self._expected_host.lower():
            raise ProviderResponseError(f"Untrusted Graph pagination host: {parsed.netloc}")
        return url

    async def get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a GET request with bearer auth, 401 refresh, and retries."""
        target_url = self._validate_url(path_or_url)
        is_continuation = bool(urlparse(path_or_url).netloc)
        request_params = None if is_continuation else params

        token = await self._auth.get_access_token()
        refreshed_401 = False
        retry_count = 0
        cumulative_delay = 0.0

        while True:
            request_headers = {
                "Authorization": f"Bearer {token}",
                "client-request-id": str(uuid.uuid4()),
                "return-client-request-id": "true",
                "User-Agent": "MailBrief/0.1.0",
                **(headers or {}),
            }

            try:
                response = await self._client.get(
                    target_url,
                    params=request_params,
                    headers=request_headers,
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if retry_count < MAX_RETRIES:
                    delay = TRANSIENT_SERVER_DELAYS[retry_count]
                    retry_count += 1
                    cumulative_delay += delay
                    await asyncio.sleep(delay)
                    continue
                raise ProviderError(f"Graph network failure: {exc}") from exc

            # 1. Handle 401 Unauthorized (silent refresh once)
            if response.status_code == 401 and not refreshed_401:
                refreshed_401 = True
                token = await self._auth.get_access_token(force_refresh=True)
                continue
            if response.status_code == 401:
                raise AuthenticationRequiredError("Microsoft session expired. Please reconnect.")

            # 2. Handle 429 Too Many Requests (Retry-After)
            if response.status_code == 429:
                retry_delay = parse_retry_after(response.headers.get("Retry-After"))
                if retry_delay is None:
                    retry_delay = (
                        TRANSIENT_SERVER_DELAYS[retry_count]
                        if retry_count < len(TRANSIENT_SERVER_DELAYS)
                        else 4.0
                    )

                if (
                    retry_count >= MAX_RETRIES
                    or (cumulative_delay + retry_delay) > MAX_CUMULATIVE_DELAY_SECONDS
                ):
                    raise ProviderRateLimitError(
                        "Microsoft Graph rate limit exceeded.",
                        retry_after_seconds=retry_delay,
                    )

                retry_count += 1
                cumulative_delay += retry_delay
                await asyncio.sleep(retry_delay)
                continue

            # 3. Handle 500, 502, 503, 504 Transient Server Errors
            if response.status_code in {500, 502, 503, 504}:
                if retry_count < MAX_RETRIES:
                    delay = (
                        parse_retry_after(response.headers.get("Retry-After"))
                        or TRANSIENT_SERVER_DELAYS[retry_count]
                    )
                    if (cumulative_delay + delay) <= MAX_CUMULATIVE_DELAY_SECONDS:
                        retry_count += 1
                        cumulative_delay += delay
                        await asyncio.sleep(delay)
                        continue
                raise ProviderResponseError(
                    f"Microsoft Graph server error ({response.status_code})."
                )

            # 4. Handle 403 Forbidden
            if response.status_code == 403:
                error_msg = self._extract_error_message(response)
                raise ProviderPermissionError(f"Microsoft Graph permission denied: {error_msg}")

            # 5. Handle Other 4xx Errors
            if 400 <= response.status_code < 500:
                error_msg = self._extract_error_message(response)
                raise ProviderResponseError(
                    f"Microsoft Graph request error ({response.status_code}): {error_msg}"
                )

            # 6. Parse JSON on Success
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ProviderResponseError("Graph response JSON must be an object.")
                return result
            except Exception as exc:
                if isinstance(exc, ProviderResponseError):
                    raise
                raise ProviderResponseError("Invalid JSON in Graph response.") from exc

    @staticmethod
    def _extract_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
            return str(payload.get("error", {}).get("message", response.text))
        except Exception:
            return response.text
