"""Owner preferences: time zones, sender rules and bounds (ADR 0014)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mailbrief.config import Settings
from mailbrief.domain.drafts import DraftLength, DraftTone
from mailbrief.domain.preferences import (
    AI_LIMIT_FIELDS,
    EXCLUSION_MAX_CHARS,
    EXCLUSIONS_MAX,
    OwnerPreferences,
    PreferencesEdit,
    is_region_zone,
    normalize_exclusion,
    sender_excluded,
)


@pytest.mark.parametrize("name", ["UTC", "America/Toronto", "America/Argentina/Buenos_Aires"])
def test_region_zones_pass(name: str) -> None:
    assert is_region_zone(name)


@pytest.mark.parametrize(
    "name",
    [
        "EST",
        "Etc/GMT+5",
        "etc/utc",
        "Mars/Base",
        "",
        "America/",
        "America/../Toronto",
        "Europe/London\n",
        "America/" + "A" * 64,
    ],
)
def test_other_zones_fail(name: str) -> None:
    assert not is_region_zone(name)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("  Alex@Example.COM ", "alex@example.com"),
        ("example.com", "@example.com"),
        ("@News.Example.com", "@news.example.com"),
        ("a@b", "a@b"),
    ],
)
def test_rules_are_normalized(text: str, rule: str) -> None:
    assert normalize_exclusion(text) == rule


@pytest.mark.parametrize(
    "text",
    [
        "",
        "@",
        "localhost",
        "@localhost",
        "alex@",
        "a@b@c",
        "@@example.com",
        "alex smith@example.com",
        "alex@exam\tple.com",
        "alex@example.com\x00",
        "alex​@example.com",
        "a@" + "b" * EXCLUSION_MAX_CHARS,
    ],
)
def test_invalid_rules_never_echo_the_text(text: str) -> None:
    with pytest.raises(ValueError) as caught:
        normalize_exclusion(text)
    assert str(caught.value) == "A sender rule must be an address or @domain."


@pytest.mark.parametrize(
    ("address", "rules", "excluded"),
    [
        ("Alex@Example.com", ("alex@example.com",), True),
        ("alex@example.com", ("@example.com",), True),
        ("news@mail.example.com", ("@example.com",), True),
        ("alex@notexample.com", ("@example.com",), False),
        ("alex@example.com.evil", ("@example.com",), False),
        ("sam@example.com", ("alex@example.com",), False),
        ("example.com", ("@example.com",), False),
        ("alex@example.com", (), False),
    ],
)
def test_sender_matching(address: str, rules: tuple[str, ...], excluded: bool) -> None:
    assert sender_excluded(address, rules) is excluded


def test_rules_are_deduplicated_keeping_the_first() -> None:
    edit = PreferencesEdit(excluded_senders=("B@example.com", "example.com", "b@EXAMPLE.com"))
    assert edit.excluded_senders == ("b@example.com", "@example.com")


def test_at_most_two_hundred_rules() -> None:
    rules = tuple(f"person{index}@example.com" for index in range(EXCLUSIONS_MAX))
    assert len(PreferencesEdit(excluded_senders=rules).excluded_senders) == EXCLUSIONS_MAX
    with pytest.raises(ValidationError):
        PreferencesEdit(excluded_senders=(*rules, "one-more@example.com"))
    # Duplicates don't count against the limit.
    assert len(PreferencesEdit(excluded_senders=(*rules, rules[0])).excluded_senders) == 200


def test_validation_errors_hide_the_owner_s_text() -> None:
    with pytest.raises(ValidationError) as caught:
        PreferencesEdit(excluded_senders=("private rule text",), time_zone="Private/Zone")
    assert "private" not in str(caught.value).casefold()


def test_defaults() -> None:
    preferences = OwnerPreferences.defaults()
    assert preferences.revision == 0 and preferences.updated_at_utc is None
    assert preferences.time_zone is None
    assert preferences.shortlist_limit == 10
    assert preferences.excluded_senders == ()
    assert (preferences.draft_tone, preferences.draft_length) == (
        DraftTone.NEUTRAL,
        DraftLength.MEDIUM,
    )
    assert all(getattr(preferences, name) is None for name in AI_LIMIT_FIELDS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shortlist_limit", 0),
        ("shortlist_limit", 11),
        ("time_zone", "EST"),
        ("ai_batch_size", 0),
        ("ai_batch_size", 11),
        ("ai_body_character_limit", 0),
        ("ai_body_character_limit", 8_001),
        ("ai_max_output_tokens", 255),
        ("ai_max_output_tokens", 64_001),
        ("ai_max_requests_per_run", 0),
        ("ai_max_requests_per_run", 1_001),
        ("ai_timeout_seconds", 9.5),
        ("ai_timeout_seconds", 600.5),
        ("draft_tone", "angry"),
    ],
)
def test_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        PreferencesEdit.model_validate({field: value})


def test_the_ai_bounds_match_the_settings() -> None:
    lowest = PreferencesEdit(
        shortlist_limit=1,
        ai_batch_size=1,
        ai_body_character_limit=1,
        ai_max_output_tokens=256,
        ai_max_requests_per_run=1,
        ai_timeout_seconds=10,
    )
    highest = PreferencesEdit(
        ai_batch_size=10,
        ai_body_character_limit=8_000,
        ai_max_output_tokens=64_000,
        ai_max_requests_per_run=1_000,
        ai_timeout_seconds=600,
    )
    assert set(AI_LIMIT_FIELDS) <= set(Settings.model_fields)
    for edit in (lowest, highest):
        Settings(**{name: getattr(edit, name) for name in AI_LIMIT_FIELDS})


def test_saved_time_is_utc_and_revision_is_not_negative() -> None:
    toronto = timezone(timedelta(hours=-4))
    saved = OwnerPreferences(revision=3, updated_at_utc=datetime(2026, 9, 29, 8, tzinfo=toronto))
    assert saved.updated_at_utc == datetime(2026, 9, 29, 12, tzinfo=UTC)
    with pytest.raises(ValidationError):
        OwnerPreferences(revision=-1)
    with pytest.raises(ValidationError):
        OwnerPreferences(revision=1, updated_at_utc=datetime(2026, 9, 29))
