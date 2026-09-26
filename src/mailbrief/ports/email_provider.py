"""Email-provider interface owned by the MailBrief application."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from mailbrief.domain.bodies import MessageBody
from mailbrief.domain.messages import AccountIdentity, MessagePage, ProviderKind


@runtime_checkable
class EmailProvider(Protocol):
    """Operations required from an email provider implementation."""

    @property
    def provider_kind(self) -> ProviderKind:
        """Return the provider represented by this adapter."""
        ...

    async def connect(self) -> AccountIdentity:
        """Connect interactively or restore an existing account session."""
        ...

    async def current_account(self) -> AccountIdentity | None:
        """Return the connected account, if one is available."""
        ...

    def iter_message_pages(
        self,
        *,
        range_start_utc: datetime,
        range_end_utc: datetime,
        continuation: str | None = None,
    ) -> AsyncIterator[MessagePage]:
        """Yield all normalized message pages in the requested UTC interval.

        ``continuation`` is an opaque ``MessagePage.continuation`` value previously yielded
        by the same provider; passing it resumes enumeration at the page it points to.
        """
        ...

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        """Fetch readable text for one shortlisted message without downloading attachments.

        Raises ``MessageUnavailableError`` when the message no longer exists.
        """
        ...

    async def disconnect(self) -> None:
        """Delete locally persisted credentials and end the session."""
        ...
