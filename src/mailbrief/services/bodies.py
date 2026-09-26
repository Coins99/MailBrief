"""Read and prepare the reviewed shortlist's bodies in memory; nothing is stored or logged."""

import asyncio
from collections.abc import Sequence
from typing import Protocol

from mailbrief.domain.bodies import (
    MAX_ANALYSIS_CHARS,
    BodySource,
    BodyStatus,
    MessageBody,
    PreparedBody,
)
from mailbrief.domain.messages import RankedMessage
from mailbrief.ports.errors import MessageUnavailableError, ProviderResponseError
from mailbrief.text.prepare import (
    looks_like_forward,
    normalize_text,
    trim_quoted_history,
    truncate_at_boundary,
)

DEFAULT_CONCURRENCY = 5


class BodyReader(Protocol):
    """The one provider capability this service needs; every EmailProvider satisfies it."""

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody: ...


def prepare_body(
    body: MessageBody, *, subject: str | None, limit: int = MAX_ANALYSIS_CHARS
) -> PreparedBody:
    """Tidy one extracted body, drop quoted history (never from forwards) and bound it."""
    text = normalize_text(body.text)
    trimmed, removed = trim_quoted_history(text, is_forward=looks_like_forward(subject))
    bounded, cut = truncate_at_boundary(trimmed, limit)
    return PreparedBody(
        provider_message_id=body.provider_message_id,
        status=BodyStatus.READY if bounded else BodyStatus.EMPTY,
        text=bounded,
        source=body.source if bounded else BodySource.NONE,
        original_chars=len(body.text),
        quoted_history_removed=removed,
        truncated=cut or body.extraction_truncated,
        attachments_skipped=body.attachments_skipped,
        unreadable_parts=body.unreadable_parts,
    )


class BodyService:
    """Fetch and prepare the reviewed shortlist with bounded concurrency."""

    def __init__(
        self,
        provider: BodyReader,
        *,
        limit: int = MAX_ANALYSIS_CHARS,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if not 1 <= limit <= MAX_ANALYSIS_CHARS:
            raise ValueError("The body limit must be between 1 and MAX_ANALYSIS_CHARS.")
        if concurrency < 1:
            raise ValueError("Concurrency must be at least 1.")
        self._provider = provider
        self._limit = limit
        self._slots = asyncio.Semaphore(concurrency)

    async def prepare(self, shortlist: Sequence[RankedMessage]) -> tuple[PreparedBody, ...]:
        """Prepared bodies in shortlist order.

        A deleted message becomes "unavailable" and an unreadable one "failed". Sign-in,
        permission and rate-limit errors stop the whole batch, and cancelling the caller
        cancels every fetch still in flight.
        """
        tasks = [asyncio.create_task(self._prepare_one(item)) for item in shortlist]
        try:
            return tuple(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _prepare_one(self, item: RankedMessage) -> PreparedBody:
        message_id = item.message.provider_message_id
        async with self._slots:
            try:
                body = await self._provider.fetch_message_body(message_id)
            except MessageUnavailableError:
                return PreparedBody(provider_message_id=message_id, status=BodyStatus.UNAVAILABLE)
            except ProviderResponseError:
                return PreparedBody(provider_message_id=message_id, status=BodyStatus.FAILED)
        return prepare_body(body, subject=item.message.subject, limit=self._limit)
