"""Bounded, read-only Gmail requests with cancellable retries."""

import asyncio
import json
import math
import random
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, cast

import httpx

from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.gmail.errors import permission_guidance

MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
MAX_RESPONSE_BYTES = 2_000_000


class TokenSource(Protocol):
    async def access_token(self) -> str: ...

    async def invalidate_access_token(self, rejected_token: str) -> None: ...


def message_id(value: object) -> str:
    """Reject path/query injection and invalid IDs before constructing requests."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        raise ProviderResponseError("Gmail returned an invalid message identifier.")
    return value


def attachment_id(value: object) -> str:
    """Accept only opaque Gmail attachment identifiers before building a request path."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", value):
        raise ProviderResponseError("Gmail returned an invalid attachment identifier.")
    return value


def retry_delay(value: str | None, attempt: int) -> float:
    """Honor finite Retry-After values; large delays are returned to the caller."""
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = math.nan
        if math.isfinite(delay):
            return max(0, delay)
    return 2.0**attempt + random.uniform(0, 0.5)


class GmailClient:
    """Read-only requests to fixed Gmail hosts; file attachments are never downloaded."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: TokenSource,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._auth = auth
        self._sleep = sleep

    async def list_messages(self, query: str, page_token: str | None) -> dict[str, object]:
        params = {
            "labelIds": "INBOX",
            "q": query,
            "maxResults": "100",
            "includeSpamTrash": "false",
            "fields": "messages(id),nextPageToken",
        }
        if page_token:
            params["pageToken"] = page_token
        result = await self._get(MESSAGES_URL, httpx.QueryParams(params), missing_ok=False)
        assert result is not None
        return result

    async def metadata(self, identifier: str) -> dict[str, object] | None:
        return await self._get(
            MESSAGES_URL + "/" + message_id(identifier),
            httpx.QueryParams(
                [
                    ("format", "metadata"),
                    ("fields", "id,threadId,labelIds,internalDate,snippet,payload/headers"),
                    *(
                        ("metadataHeaders", name)
                        for name in ("From", "To", "Subject", "Message-ID")
                    ),
                ]
            ),
            missing_ok=True,
        )

    async def message(self, identifier: str) -> dict[str, object] | None:
        """One message's MIME structure; attachment data stays behind attachment IDs."""
        return await self._get(
            MESSAGES_URL + "/" + message_id(identifier),
            httpx.QueryParams([("format", "full"), ("fields", "id,payload")]),
            missing_ok=True,
        )

    async def part_data(self, identifier: str, attachment: str) -> dict[str, object] | None:
        """A separately stored text part. Callers must never use this for file attachments."""
        return await self._get(
            f"{MESSAGES_URL}/{message_id(identifier)}/attachments/{attachment_id(attachment)}",
            httpx.QueryParams([("fields", "size,data")]),
            missing_ok=True,
        )

    async def _get(
        self, url: str, params: httpx.QueryParams, *, missing_ok: bool
    ) -> dict[str, object] | None:
        refreshed = False
        for attempt in range(4):
            token = await self._auth.access_token()
            try:
                async with self._http.stream(
                    "GET",
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    follow_redirects=False,
                    timeout=30,
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ProviderResponseError("Gmail response exceeded its size limit.")
                    status = response.status_code
                    after = response.headers.get("Retry-After")
            except httpx.HTTPError:
                if attempt == 3:
                    raise ProviderResponseError("Gmail request failed; retry later.") from None
                await self._sleep(retry_delay(None, attempt))
                continue
            if status == 401:
                if refreshed or attempt == 3:
                    raise AuthenticationRequiredError("Gmail authorization rejected; reconnect.")
                await self._auth.invalidate_access_token(token)
                refreshed = True
                continue
            if status == 404 and missing_ok:
                return None
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = None
            rate_limited = status == 429
            if status == 403 and isinstance(payload, dict):
                error = payload.get("error")
                errors = error.get("errors", []) if isinstance(error, dict) else []
                if isinstance(errors, list):
                    rate_limited = any(
                        isinstance(item, dict)
                        and item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
                        for item in errors
                    )
            if rate_limited or status in {500, 502, 503, 504}:
                delay = retry_delay(after, attempt)
                if attempt == 3 or delay > 30:
                    if rate_limited:
                        raise ProviderRateLimitError(
                            "Gmail rate limit reached; retry later.", retry_after_seconds=delay
                        )
                    raise ProviderResponseError("Gmail is temporarily unavailable; retry later.")
                await self._sleep(delay)
                continue
            if status == 403:
                raise ProviderPermissionError(permission_guidance(payload))
            if status != 200 or not isinstance(payload, dict):
                raise ProviderResponseError("Gmail returned an invalid response.")
            return cast(dict[str, object], payload)
        raise ProviderResponseError("Gmail retry limit reached.")
