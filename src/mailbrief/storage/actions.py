"""Map stored action suggestions, and the owner's decisions on them, to domain models.

repositories.py imports this module, so it must never import repositories.py.
"""

from mailbrief.domain.analysis import (
    ActionEffort,
    ActionOwnership,
    ActionSuggestion,
    DeadlinePrecision,
    TargetReason,
)
from mailbrief.storage.tables import ActionSuggestionTable


def suggestion_from_row(row: ActionSuggestionTable) -> ActionSuggestion:
    """Rebuild a stored suggestion; raises ValueError when the row no longer validates."""
    if not isinstance(row.steps_json, list):
        raise ValueError("stored suggestion steps must be a list")
    return ActionSuggestion(
        position=row.position,
        title=row.title,
        ownership=ActionOwnership(row.ownership),
        effort=None if row.effort is None else ActionEffort(row.effort),
        deadline_text=row.deadline_text,
        deadline_precision=DeadlinePrecision(row.deadline_precision),
        deadline_date=row.deadline_date,
        deadline_at_utc=row.deadline_at_utc,
        deadline_timezone=row.deadline_timezone,
        suggested_target_date=row.suggested_target_date,
        target_reason=None if row.target_reason is None else TargetReason(row.target_reason),
        steps=tuple(row.steps_json),
        evidence=row.evidence,
        fingerprint=row.fingerprint,
    )
