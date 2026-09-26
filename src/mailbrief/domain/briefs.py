"""Brief-generation contracts shared by the analysis, digest and brief services."""

from enum import StrEnum


class AnalysisOutcome(StrEnum):
    """What happened to one shortlisted message during brief generation."""

    ANALYZED = "analyzed"  # A new provider result from this run.
    REUSED = "reused"  # A valid cached analysis.
    FAILED = "failed"  # A body failure, or no valid result.
    SKIPPED = "skipped"  # An empty or unavailable body; never sent.
