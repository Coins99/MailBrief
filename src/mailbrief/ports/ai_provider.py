"""AI-provider interface owned by the MailBrief application."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from mailbrief.domain.analysis import AnalysisRequest, AnalysisResponse


@runtime_checkable
class AIProvider(Protocol):
    """Structured message analysis used by the analysis service."""

    @property
    def provider_name(self) -> str:
        """Return a stable provider name for cache keys, consent and diagnostics."""
        ...

    @property
    def model_name(self) -> str:
        """Return the model identifier recorded in cache keys and brief coverage."""
        ...

    @property
    def prompt_version(self) -> str:
        """Return the prompt version recorded in cache keys."""
        ...

    @property
    def requests_sent(self) -> int:
        """Return the HTTP attempts made so far, including failed and retried ones."""
        ...

    async def credentials_available(self) -> bool:
        """Whether a usable API key can be loaded; never raises for a missing or unreadable key."""
        ...

    async def analyze(self, requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
        """Analyze one bounded batch with one logical request per call (the adapter may retry it).

        The request message keys are unique within the batch. Candidates come back
        unvalidated; the analysis service validates them. Authentication, permission,
        rate-limit, timeout and network failures raise ``mailbrief.ports.errors`` types,
        and those errors never contain email content.
        """
        ...
