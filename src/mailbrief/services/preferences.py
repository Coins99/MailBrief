"""The owner's preferences: read, save, reset and apply them (ADR 0014).

Preferences are one revisioned row shared by the desktop and the CLI. Unreadable
preferences fail closed: callers that could send data raise PreferencesUnavailableError
instead of falling back to defaults, which would drop the owner's sender exclusions.
Sender rules are the owner's own text; only their count is ever logged.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.config import Settings
from mailbrief.domain.common import normalize_utc, utc_now
from mailbrief.domain.preferences import AI_LIMIT_FIELDS, OwnerPreferences, PreferencesEdit
from mailbrief.errors import ConfigurationError
from mailbrief.services.calendar import resolve_timezone
from mailbrief.storage.preferences import (
    PREFERENCES_ROW_ID,
    PreferencesRepository,
    apply_edit,
    preferences_from_row,
)
from mailbrief.storage.tables import OwnerPreferencesTable

logger = logging.getLogger(__name__)

_CONFLICT: Final = "Preferences changed since they were loaded; reopen Settings."
_UNAVAILABLE: Final = (
    "Saved preferences could not be read. Open Settings > Preferences and reset them."
)

LimitSource = Literal["environment", "saved", "default"]


class PreferencesConflictError(ValueError):
    """The preferences changed since the caller read them."""

    def __init__(self) -> None:
        super().__init__(_CONFLICT)


class PreferencesUnavailableError(ConfigurationError):
    """The saved preferences can't be read, so nothing that could send data may run."""

    def __init__(self) -> None:
        super().__init__(_UNAVAILABLE)


class PreferencesService:
    """Every mutating method reads the clock once, commits once and rolls back on failure."""

    def __init__(self, session: AsyncSession, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._session = session
        self._clock = clock
        self._repository = PreferencesRepository(session)

    async def get(self) -> OwnerPreferences:
        """The saved preferences, or the defaults (revision 0) when never saved."""
        try:
            return preferences_from_row(await self._repository.get_row())
        except ValueError:
            logger.warning("Saved preferences could not be read")
            raise PreferencesUnavailableError() from None

    async def save(self, edit: PreferencesEdit, expected_revision: int) -> OwnerPreferences:
        """Replace the preferences; ``expected_revision`` is the revision the caller saw,
        0 when they were never saved. A different revision raises PreferencesConflictError."""

        async def operation(now: datetime) -> OwnerPreferences:
            row = await self._repository.get_row_to_replace()
            if (0 if row is None else row.revision) != expected_revision:
                raise PreferencesConflictError()
            return self._replace(row, edit, now)

        saved = await self._write(operation)
        logger.info("Preferences saved with %d sender rules", len(saved.excluded_senders))
        return saved

    async def reset(self) -> OwnerPreferences:
        """Back to the defaults, whatever the saved revision; an unreadable row is replaced."""

        async def operation(now: datetime) -> OwnerPreferences:
            return self._replace(await self._repository.get_row_to_replace(), None, now)

        reset = await self._write(operation)
        logger.info("Preferences reset to the defaults")
        return reset

    def _replace(
        self, row: OwnerPreferencesTable | None, edit: PreferencesEdit | None, now: datetime
    ) -> OwnerPreferences:
        if row is None:
            row = OwnerPreferencesTable(id=PREFERENCES_ROW_ID, revision=0)
            self._repository.add(row)
        apply_edit(row, edit or PreferencesEdit())
        row.revision += 1
        row.updated_at_utc = now
        return preferences_from_row(row)

    async def _write(
        self, operation: Callable[[datetime], Awaitable[OwnerPreferences]]
    ) -> OwnerPreferences:
        now = normalize_utc(self._clock())
        try:
            result = await operation(now)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return result


def effective_settings(settings: Settings, preferences: OwnerPreferences) -> Settings:
    """Settings with the saved AI limits applied where no MAILBRIEF_* variable set them.

    Only AI_LIMIT_FIELDS are considered: other fields may be in ``model_fields_set``
    because they were passed by name, as DesktopPreferences.settings() does.
    """
    update = {
        name: value
        for name in AI_LIMIT_FIELDS
        if name not in settings.model_fields_set
        and (value := getattr(preferences, name)) is not None
    }
    return settings.model_copy(update=update) if update else settings


@dataclass(frozen=True, slots=True)
class AILimit:
    """One AI limit as it applies, and where its value came from."""

    name: str
    value: float
    source: LimitSource


def ai_limits(settings: Settings, preferences: OwnerPreferences) -> tuple[AILimit, ...]:
    """Each AI limit's value and source: environment, then saved, then default."""
    effective = effective_settings(settings, preferences)
    limits: list[AILimit] = []
    for name in AI_LIMIT_FIELDS:
        source: LimitSource
        if name in settings.model_fields_set:
            source = "environment"
        elif getattr(preferences, name) is not None:
            source = "saved"
        else:
            source = "default"
        limits.append(AILimit(name=name, value=getattr(effective, name), source=source))
    return tuple(limits)


def owner_zone(preferences: OwnerPreferences, explicit: str | None = None) -> ZoneInfo:
    """An explicit zone, else the saved one, else the system time zone.

    Raises InvalidTimezoneError when an explicit zone is unknown.
    """
    return resolve_timezone(explicit or preferences.time_zone)
