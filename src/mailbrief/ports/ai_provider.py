"""AI-provider interface owned by the MailBrief application."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from mailbrief.domain.analysis import AnalysisRequest, MessageAnalysis


@runtime_checkable
class AIProvider(Protocol):
    """Structured message analysis required by the digest service."""

    @property
    def provider_name(self) -> str:
        """Return a stable provider name for cache keys and diagnostics."""
        ...

    async def analyze(
        self,
        requests: Sequence[AnalysisRequest],
    ) -> tuple[MessageAnalysis, ...]:
        """Analyze one bounded batch and return validated results."""
        ...
