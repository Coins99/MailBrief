"""The AI drafting interface owned by the MailBrief application (ADR 0013)."""

from typing import Protocol, runtime_checkable

from mailbrief.domain.drafting import DraftingRequest, DraftingResponse


@runtime_checkable
class DraftingProvider(Protocol):
    """Writes one draft from exactly the context in a DraftingRequest."""

    @property
    def provider_name(self) -> str:
        """A stable provider name for consent and generation records."""
        ...

    @property
    def model_name(self) -> str:
        """The model recorded with each generated version."""
        ...

    @property
    def drafting_prompt_version(self) -> str:
        """The drafting prompt version recorded with each generated version."""
        ...

    @property
    def privacy_notice(self) -> str:
        """The provider's data-handling notice, shown before the owner approves."""
        ...

    @property
    def requests_sent(self) -> int:
        """HTTP attempts made so far, including failed and retried ones."""
        ...

    async def credentials_available(self) -> bool:
        """Whether a usable API key can be loaded; never raises for a missing key."""
        ...

    async def draft(self, request: DraftingRequest) -> DraftingResponse:
        """One logical call (the adapter may retry transient failures).

        The candidate comes back unvalidated; the drafting service validates it. Provider
        failures raise ``mailbrief.ports.errors`` types, which never contain draft or email
        text.
        """
        ...
