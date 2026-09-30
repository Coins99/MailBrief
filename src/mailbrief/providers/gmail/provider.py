"""Gmail Inbox pagination, bounded metadata retrieval, thread metadata and shortlisted body
reading."""

import asyncio
import math
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from mailbrief.domain.bodies import MessageBody
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.messages import AccountIdentity, MessagePage, NormalizedMessage, ProviderKind
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    MessageUnavailableError,
    ProviderResponseError,
)
from mailbrief.providers.gmail.body import extract_body
from mailbrief.providers.gmail.client import GmailClient, message_id
from mailbrief.providers.gmail.errors import response_error
from mailbrief.providers.gmail.mapper import map_metadata

# A tracked thread never counts drafts, trashed or spam messages (ADR 0015).
_IGNORED_LABELS = frozenset({"DRAFT", "TRASH", "SPAM"})


class GmailSession(Protocol):
    async def connect(self, *, silent_only: bool = False) -> AccountIdentity: ...

    async def disconnect(self) -> None: ...


class GmailProvider:
    def __init__(
        self, auth: GmailSession, client: GmailClient, *, silent_only: bool = False
    ) -> None:
        self._auth = auth
        self._client = client
        self._silent_only = silent_only
        self._account: AccountIdentity | None = None
        self._slots = asyncio.Semaphore(5)

    @property
    def provider_kind(self) -> ProviderKind:
        return ProviderKind.GMAIL

    async def connect(self) -> AccountIdentity:
        self._account = await self._auth.connect(silent_only=self._silent_only)
        return self._account

    async def current_account(self) -> AccountIdentity | None:
        return self._account

    async def disconnect(self) -> None:
        self._account = None
        await self._auth.disconnect()

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        """Readable text of one shortlisted message; attachments are never downloaded."""
        identifier = message_id(provider_message_id)
        async with self._slots:
            raw = await self._client.message(identifier)
            if raw is None:
                raise MessageUnavailableError("The Gmail message is no longer available.")

            async def fetch_part(attachment: str) -> dict[str, object] | None:
                return await self._client.part_data(identifier, attachment)

            return await extract_body(raw, identifier, fetch_part)

    async def fetch_thread(self, provider_thread_id: str) -> tuple[NormalizedMessage, ...]:
        """A thread's message metadata, oldest first; never bodies or attachments.

        Drafts, trash and spam are skipped, as are items from another thread and items that
        can't be mapped: one bad message never fails the thread. No Inbox or date filtering
        happens here. A thread that no longer exists raises MessageUnavailableError.
        """
        if self._account is None:
            raise AuthenticationRequiredError("Connect Gmail before reading threads.")
        identifier = message_id(provider_thread_id)
        account = self._account
        async with self._slots:
            raw = await self._client.thread(identifier)
        if raw is None:
            raise MessageUnavailableError("The Gmail thread is no longer available.")
        items = raw.get("messages", [])
        if not isinstance(items, list):
            raise response_error("Gmail returned an invalid thread.")
        messages: list[NormalizedMessage] = []
        for item in items:
            if not isinstance(item, dict) or item.get("threadId") != identifier:
                continue
            labels = item.get("labelIds", [])
            if isinstance(labels, list) and _IGNORED_LABELS.intersection(
                label for label in labels if isinstance(label, str)
            ):
                continue
            try:
                messages.append(map_metadata(item, account))
            except ProviderResponseError:
                continue
        return tuple(
            sorted(messages, key=lambda item: (item.received_at_utc, item.provider_message_id))
        )

    async def iter_message_pages(
        self,
        *,
        range_start_utc: datetime,
        range_end_utc: datetime,
        continuation: str | None = None,
    ) -> AsyncIterator[MessagePage]:
        if self._account is None:
            raise AuthenticationRequiredError("Connect Gmail before synchronizing.")
        start, end = normalize_utc(range_start_utc), normalize_utc(range_end_utc)
        if end <= start:
            raise ValueError("The Gmail time window must have a positive duration.")
        query = f"after:{math.floor(start.timestamp()) - 1} before:{math.ceil(end.timestamp()) + 1}"
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        if continuation is not None:
            if not continuation or len(continuation) > 4096:
                raise response_error("Invalid Gmail continuation token.")
            seen_tokens.add(continuation)
            token = continuation
        number = 0
        account = self._account

        async def fetch(identifier: str) -> NormalizedMessage | None | ProviderResponseError:
            async with self._slots:
                try:
                    raw = await self._client.metadata(identifier)
                    if raw is None:
                        return None  # Mail deleted between list and get.
                    message = map_metadata(raw, account)
                    if message.provider_message_id != identifier:
                        raise response_error("Gmail returned mismatched message metadata.")
                    if not message.is_in_inbox or not start <= message.received_at_utc < end:
                        return None
                    return message
                except ProviderResponseError as exc:
                    return exc  # Preserve other successful items, but prohibit reconciliation.

        while True:
            page = await self._client.list_messages(query, token)
            raw_ids = page.get("messages", [])
            next_token = page.get("nextPageToken")
            if (
                not isinstance(raw_ids, list)
                or len(raw_ids) > 500
                or (
                    next_token is not None
                    and (
                        not isinstance(next_token, str) or not next_token or len(next_token) > 4096
                    )
                )
            ):
                raise response_error("Gmail returned an invalid message page.")
            identifiers: list[str] = []
            for item in raw_ids:
                if not isinstance(item, dict):
                    raise response_error("Gmail returned an invalid message list.")
                identifier = message_id(item.get("id"))
                if identifier not in seen_ids:
                    seen_ids.add(identifier)
                    identifiers.append(identifier)
            tasks = [asyncio.create_task(fetch(identifier)) for identifier in identifiers]
            try:
                results = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            number += 1
            yield MessagePage(
                page_number=number,
                messages=tuple(item for item in results if isinstance(item, NormalizedMessage)),
                failed_message_count=sum(
                    isinstance(item, ProviderResponseError) for item in results
                ),
                continuation=next_token,
            )
            if next_token is None:
                break
            if next_token in seen_tokens:
                raise response_error("Gmail repeated a pagination token; retry synchronization.")
            seen_tokens.add(next_token)
            token = next_token
