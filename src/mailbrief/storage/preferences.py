"""The owner's preferences row (ADR 0014); the service owns commits and rollbacks.

Rows change only through ORM objects.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.storage.tables import OwnerPreferencesTable

PREFERENCES_ROW_ID = 1


class PreferencesRepository:
    """Read and add the single preferences row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_row(self) -> OwnerPreferencesTable | None:
        """The row with every column freshly read, or None when never saved.

        Raises ValueError when a stored JSON value can't be decoded.
        """
        result = await self._session.execute(
            select(OwnerPreferencesTable)
            .where(OwnerPreferencesTable.id == PREFERENCES_ROW_ID)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_row_to_replace(self) -> OwnerPreferencesTable | None:
        """The row with only its identity and revision read.

        A save or reset replaces every other column, so an unreadable row can still be
        replaced.
        """
        result = await self._session.execute(
            select(OwnerPreferencesTable)
            .where(OwnerPreferencesTable.id == PREFERENCES_ROW_ID)
            .options(load_only(OwnerPreferencesTable.id, OwnerPreferencesTable.revision))
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def add(self, row: OwnerPreferencesTable) -> None:
        self._session.add(row)


def preferences_from_row(row: OwnerPreferencesTable | None) -> OwnerPreferences:
    """The domain preferences; None gives the defaults.

    Raises ValueError with a static message when the row no longer validates.
    """
    if row is None:
        return OwnerPreferences.defaults()
    rules = row.excluded_senders_json
    if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
        raise ValueError("Saved preferences are invalid.")
    try:
        return OwnerPreferences(
            time_zone=row.time_zone,
            shortlist_limit=row.shortlist_limit,
            excluded_senders=tuple(rules),
            draft_tone=row.draft_tone,
            draft_length=row.draft_length,
            ai_batch_size=row.ai_batch_size,
            ai_body_character_limit=row.ai_body_character_limit,
            ai_max_output_tokens=row.ai_max_output_tokens,
            ai_max_requests_per_run=row.ai_max_requests_per_run,
            ai_timeout_seconds=row.ai_timeout_seconds,
            refresh_on_launch=row.refresh_on_launch,
            refresh_interval_minutes=row.refresh_interval_minutes,
            revision=row.revision,
            updated_at_utc=row.updated_at_utc,
        )
    except ValueError:
        raise ValueError("Saved preferences are invalid.") from None


def apply_edit(row: OwnerPreferencesTable, edit: PreferencesEdit) -> None:
    """Replace every preference column with the edit's values."""
    row.time_zone = edit.time_zone
    row.shortlist_limit = edit.shortlist_limit
    row.excluded_senders_json = list(edit.excluded_senders)
    row.draft_tone = edit.draft_tone.value
    row.draft_length = edit.draft_length.value
    row.ai_batch_size = edit.ai_batch_size
    row.ai_body_character_limit = edit.ai_body_character_limit
    row.ai_max_output_tokens = edit.ai_max_output_tokens
    row.ai_max_requests_per_run = edit.ai_max_requests_per_run
    row.ai_timeout_seconds = edit.ai_timeout_seconds
    row.refresh_on_launch = edit.refresh_on_launch
    row.refresh_interval_minutes = edit.refresh_interval_minutes
