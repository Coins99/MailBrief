"""Preferences service: revisions, resets, failing closed, precedence and the owner's zone."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.config import Settings
from mailbrief.domain.drafts import DraftTone
from mailbrief.domain.preferences import AI_LIMIT_FIELDS, OwnerPreferences, PreferencesEdit
from mailbrief.errors import ConfigurationError
from mailbrief.services import preferences as preferences_module
from mailbrief.services.calendar import InvalidTimezoneError
from mailbrief.services.preferences import (
    AILimit,
    PreferencesConflictError,
    PreferencesService,
    PreferencesUnavailableError,
    ai_limits,
    effective_settings,
    owner_zone,
)
from mailbrief.storage.database import Database
from mailbrief.ui.preferences import DesktopPreferences

AT = datetime(2026, 9, 29, 13, tzinfo=UTC)
RULES = ("private-sender@example.com", "@private-domain.example")


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "preferences.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


def service(session: AsyncSession, at: datetime = AT) -> PreferencesService:
    return PreferencesService(session, clock=lambda: at)


async def test_a_fresh_database_gives_the_defaults(session: AsyncSession) -> None:
    assert await service(session).get() == OwnerPreferences.defaults()


async def test_the_first_save_inserts_revision_one(
    database: Database, session: AsyncSession
) -> None:
    saved = await service(session).save(
        PreferencesEdit(time_zone="America/Toronto", shortlist_limit=3), 0
    )
    assert (saved.revision, saved.updated_at_utc, saved.shortlist_limit) == (1, AT, 3)
    async with database.session() as other:
        assert await PreferencesService(other).get() == saved


async def test_a_matching_revision_applies_and_bumps(session: AsyncSession) -> None:
    preferences = service(session)
    await preferences.save(PreferencesEdit(shortlist_limit=3), 0)
    later = AT + timedelta(minutes=5)
    saved = await service(session, later).save(PreferencesEdit(draft_tone=DraftTone.WARM), 1)
    assert (saved.revision, saved.updated_at_utc) == (2, later)
    assert (saved.shortlist_limit, saved.draft_tone) == (10, DraftTone.WARM)


@pytest.mark.parametrize("stale", [0, 2])
async def test_a_stale_revision_is_a_conflict(session: AsyncSession, stale: int) -> None:
    preferences = service(session)
    kept = await preferences.save(PreferencesEdit(shortlist_limit=3), 0)
    with pytest.raises(PreferencesConflictError, match="reopen Settings"):
        await preferences.save(PreferencesEdit(shortlist_limit=5), stale)
    assert await preferences.get() == kept


async def test_saving_before_the_first_save_needs_revision_zero(session: AsyncSession) -> None:
    preferences = service(session)
    with pytest.raises(PreferencesConflictError):
        await preferences.save(PreferencesEdit(), 1)
    assert await preferences.get() == OwnerPreferences.defaults()


async def test_reset_needs_no_revision_and_bumps(session: AsyncSession) -> None:
    preferences = service(session)
    await preferences.save(
        PreferencesEdit(
            time_zone="America/Toronto",
            excluded_senders=RULES,
            ai_batch_size=3,
            ai_timeout_seconds=30,
        ),
        0,
    )
    reset = await preferences.reset()
    assert reset == OwnerPreferences(revision=2, updated_at_utc=AT)
    assert await preferences.get() == reset


async def test_reset_before_any_save_writes_the_defaults(session: AsyncSession) -> None:
    reset = await service(session).reset()
    assert reset == OwnerPreferences(revision=1, updated_at_utc=AT)


async def test_the_refresh_settings_save_reload_and_reset(
    database: Database, session: AsyncSession
) -> None:
    preferences = service(session)
    saved = await preferences.save(
        PreferencesEdit(refresh_on_launch=True, refresh_interval_minutes=120), 0
    )
    assert (saved.refresh_on_launch, saved.refresh_interval_minutes) == (True, 120)
    async with database.session() as other:
        again = await PreferencesService(other).get()
    assert again == saved

    changed = await preferences.save(PreferencesEdit(refresh_interval_minutes=None), 1)
    assert (changed.refresh_on_launch, changed.refresh_interval_minutes) == (False, None)

    await preferences.save(PreferencesEdit(refresh_on_launch=True, refresh_interval_minutes=60), 2)
    reset = await preferences.reset()  # No automatic refresh is the default.
    assert (reset.refresh_on_launch, reset.refresh_interval_minutes) == (False, None)


async def test_the_default_clock_is_utc(session: AsyncSession) -> None:
    before = datetime.now(UTC)
    reset = await PreferencesService(session).reset()
    assert reset.updated_at_utc is not None and reset.updated_at_utc >= before
    assert reset.updated_at_utc.tzinfo is UTC


@pytest.mark.parametrize(
    "corruption",
    ["excluded_senders_json = 'not json'", "time_zone = 'Mars/Base'"],
)
async def test_unreadable_preferences_fail_closed_until_reset(
    session: AsyncSession, corruption: str, caplog: pytest.LogCaptureFixture
) -> None:
    preferences = service(session)
    await preferences.save(PreferencesEdit(excluded_senders=RULES), 0)
    await session.execute(text(f"UPDATE owner_preferences SET {corruption}"))
    await session.commit()

    with caplog.at_level(logging.DEBUG), pytest.raises(PreferencesUnavailableError) as caught:
        await preferences.get()
    await session.rollback()
    assert isinstance(caught.value, ConfigurationError)
    assert "Settings > Preferences" in str(caught.value)

    assert (await preferences.reset()).revision == 2
    assert (await preferences.get()).excluded_senders == ()
    assert "private" not in caplog.text


async def test_a_failed_write_rolls_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferences = service(session)
    await preferences.save(PreferencesEdit(shortlist_limit=3), 0)

    def broken(*args: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(preferences_module, "apply_edit", broken)
    with pytest.raises(RuntimeError):
        await preferences.save(PreferencesEdit(shortlist_limit=5), 1)
    monkeypatch.undo()
    assert (await preferences.get()).shortlist_limit == 3


async def test_logs_count_rules_but_never_show_them(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="mailbrief"):
        await service(session).save(PreferencesEdit(excluded_senders=RULES), 0)
    assert "2 sender rules" in caplog.text
    assert "private" not in caplog.text


def clear_ai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AI_LIMIT_FIELDS:
        monkeypatch.delenv(f"MAILBRIEF_{name.upper()}", raising=False)


SAVED = OwnerPreferences(
    revision=1,
    ai_batch_size=3,
    ai_body_character_limit=2_000,
    ai_max_output_tokens=1_024,
    ai_max_requests_per_run=4,
    ai_timeout_seconds=45,
)


@pytest.mark.parametrize("build", ["settings", "desktop"])
def test_an_environment_value_is_explicit_even_when_it_equals_the_default(
    monkeypatch: pytest.MonkeyPatch, build: str
) -> None:
    """pydantic-settings counts environment values in model_fields_set; precedence
    relies on it, for Settings() and for the desktop's settings() alike."""
    clear_ai_environment(monkeypatch)
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "1")  # The built-in default.
    monkeypatch.setenv("MAILBRIEF_AI_TIMEOUT_SECONDS", "120")  # The built-in default.
    settings = Settings() if build == "settings" else DesktopPreferences(groq_model="m").settings()
    assert {"ai_batch_size", "ai_timeout_seconds"} <= settings.model_fields_set
    assert "ai_max_output_tokens" not in settings.model_fields_set

    effective = effective_settings(settings, SAVED)

    assert (effective.ai_batch_size, effective.ai_timeout_seconds) == (1, 120)  # Environment.
    assert effective.ai_body_character_limit == 2_000  # Saved.
    assert effective.ai_max_output_tokens == 1_024
    assert effective.ai_max_requests_per_run == 4


def test_only_ai_limits_follow_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    """The desktop passes the OAuth path and model by name; that says nothing about AI."""
    clear_ai_environment(monkeypatch)
    settings = DesktopPreferences(gmail_oauth_client_path=None, groq_model=None).settings()
    assert {"gmail_oauth_client_path", "groq_model"} <= settings.model_fields_set

    effective = effective_settings(settings, SAVED)

    assert effective.ai_batch_size == 3
    assert (effective.gmail_oauth_client_path, effective.groq_model) == (None, None)


def test_defaults_leave_settings_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_ai_environment(monkeypatch)
    settings = Settings()
    assert effective_settings(settings, OwnerPreferences.defaults()) is settings


def test_each_limit_names_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_ai_environment(monkeypatch)
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "5")
    saved = OwnerPreferences(revision=1, ai_batch_size=3, ai_body_character_limit=2_000)
    assert ai_limits(Settings(), saved) == (
        AILimit("ai_batch_size", 5, "environment"),
        AILimit("ai_body_character_limit", 2_000, "saved"),
        AILimit("ai_max_output_tokens", 4_000, "default"),
        AILimit("ai_max_requests_per_run", 10, "default"),
        AILimit("ai_timeout_seconds", 120, "default"),
    )


def test_owner_zone_prefers_explicit_then_saved_then_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("Asia/Tokyo"))
    saved = OwnerPreferences(revision=1, time_zone="America/Toronto")
    assert owner_zone(saved, "Europe/Paris") == ZoneInfo("Europe/Paris")
    assert owner_zone(saved) == ZoneInfo("America/Toronto")
    assert owner_zone(OwnerPreferences.defaults()) == ZoneInfo("Asia/Tokyo")
    with pytest.raises(InvalidTimezoneError):
        owner_zone(saved, "Mars/Base")
