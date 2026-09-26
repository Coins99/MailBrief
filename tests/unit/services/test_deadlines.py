"""Deadline resolution rules D1-D7, DST edges and stated zones."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mailbrief.domain.analysis import AnalysisCandidate, AnalysisRequest, DeadlinePrecision
from mailbrief.domain.messages import EmailContact
from mailbrief.services.deadlines import (
    ZONE_TOKEN_PATTERN,
    InvalidDeadlineError,
    ResolvedDeadline,
    resolve_deadline,
)

ZONE = "America/Toronto"
BODY = (
    "Please approve the budget by Friday 5 PM. Reply ASAP. The board meets 8 March at 2:30, "
    "the audit call is 1 November at 1:30, and London wants it by 5 PM London time or 5 PM PT."
)
SEPTEMBER_2 = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
NO_DEADLINE = ResolvedDeadline(None, DeadlinePrecision.NONE, None, None, None)


def request(received: datetime = SEPTEMBER_2, **overrides: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "message_key": "0000abcd",
        "subject": "Budget approval",
        "sender": EmailContact(address="alex@example.com"),
        "received_at_utc": received,
        "timezone_name": ZONE,
        "body_text": BODY,
    }
    values.update(overrides)
    return AnalysisRequest.model_validate(values)


def candidate(**overrides: object) -> AnalysisCandidate:
    values: dict[str, object] = {
        "message_key": "0000abcd",
        "category": "deadline",
        "summary": "Approve the budget.",
        "action_required": False,
        "action_text": None,
        "deadline_text": None,
        "deadline_date": None,
        "deadline_time": None,
        "stated_timezone": None,
        "confidence": 0.9,
        "evidence": "Please approve the budget",
    }
    values.update(overrides)
    return AnalysisCandidate.model_validate(values)


def friday(**overrides: object) -> AnalysisCandidate:
    values: dict[str, object] = {"deadline_text": "Friday 5 PM", "deadline_date": "2026-09-04"}
    return candidate(**(values | overrides))


@pytest.mark.parametrize("text", [None, "", "   "])
def test_d1_no_text_means_no_deadline_and_ignores_the_stated_zone(text: str | None) -> None:
    resolved = resolve_deadline(
        candidate(deadline_text=text, stated_timezone="Europe/London"), request()
    )

    assert resolved == NO_DEADLINE


@pytest.mark.parametrize("fields", [{"deadline_date": "2026-09-04"}, {"deadline_time": "17:00"}])
def test_d1_a_date_or_time_without_text_is_invalid(fields: dict[str, object]) -> None:
    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(candidate(**fields), request())


def test_d2_a_phrase_that_is_not_in_the_email_is_invalid() -> None:
    with pytest.raises(InvalidDeadlineError) as caught:
        resolve_deadline(candidate(deadline_text="next Tuesday at noon"), request())

    assert "Tuesday" not in str(caught.value)


def test_d2_text_over_500_characters_is_invalid_even_when_quoted() -> None:
    long_phrase = "y" * 501

    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(
            candidate(deadline_text=long_phrase), request(body_text=f"{long_phrase} end")
        )


def test_d2_d3_text_quoted_from_the_subject_without_a_date_is_unresolved() -> None:
    resolved = resolve_deadline(candidate(deadline_text="budget APPROVAL"), request())

    assert resolved == ResolvedDeadline(
        "budget APPROVAL", DeadlinePrecision.UNRESOLVED, None, None, None
    )


def test_d3_blank_date_and_time_leave_an_unresolved_phrase() -> None:
    resolved = resolve_deadline(
        candidate(deadline_text="ASAP", deadline_date=" ", deadline_time=""), request()
    )

    assert resolved == ResolvedDeadline("ASAP", DeadlinePrecision.UNRESOLVED, None, None, None)


def test_d3_a_time_without_a_date_leaves_an_unresolved_phrase() -> None:
    resolved = resolve_deadline(candidate(deadline_text="ASAP", deadline_time="17:00"), request())

    assert resolved == ResolvedDeadline("ASAP", DeadlinePrecision.UNRESOLVED, None, None, None)


@pytest.mark.parametrize(
    "raw", ["2026/09/04", "Sept 4", "2026-9-4", "20260904", "2026-02-30", "２026-09-04"]
)
def test_d4_an_unusable_date_leaves_an_unresolved_phrase(raw: str) -> None:
    resolved = resolve_deadline(friday(deadline_date=raw, deadline_time="17:00"), request())

    assert resolved == ResolvedDeadline(
        "Friday 5 PM", DeadlinePrecision.UNRESOLVED, None, None, None
    )


@pytest.mark.parametrize(
    ("raw", "valid"),
    [("2026-08-01", False), ("2026-08-02", True), ("2027-09-03", True), ("2027-09-04", False)],
)
def test_d4_dates_outside_the_window_around_the_local_received_day_are_unresolved(
    raw: str, valid: bool
) -> None:
    late_evening_in_toronto = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)  # 2 September, 22:00
    deadline = candidate(deadline_text="Friday 5 PM", deadline_date=raw)

    resolved = resolve_deadline(deadline, request(late_evening_in_toronto))

    if valid:
        assert (resolved.precision, resolved.date) == (
            DeadlinePrecision.DATE,
            date.fromisoformat(raw),
        )
    else:
        assert (resolved.precision, resolved.date) == (DeadlinePrecision.UNRESOLVED, None)


def test_d5_a_date_without_a_time_is_a_day_in_the_user_zone() -> None:
    resolved = resolve_deadline(friday(stated_timezone="Europe/London"), request())

    assert resolved == ResolvedDeadline(
        "Friday 5 PM", DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


@pytest.mark.parametrize(
    "raw", ["24:00", "23:60", "12:00:60", "7pm", "123:00", ":30", "17", "17:00 PM"]
)
def test_d6_an_unusable_time_keeps_only_the_date(raw: str) -> None:
    resolved = resolve_deadline(friday(deadline_time=raw), request())

    assert resolved == ResolvedDeadline(
        "Friday 5 PM", DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


def test_d6_a_one_digit_hour_parses() -> None:
    resolved = resolve_deadline(friday(deadline_time="9:00"), request())

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc == datetime(2026, 9, 4, 13, 0, tzinfo=UTC)


def test_d6_d7_an_exact_time_resolves_in_the_user_zone_without_seconds() -> None:
    resolved = resolve_deadline(friday(deadline_time="17:00:45"), request())

    assert resolved == ResolvedDeadline(
        "Friday 5 PM",
        DeadlinePrecision.DATETIME,
        date(2026, 9, 4),
        datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
        ZONE,
    )


def test_d6_a_stated_iana_zone_is_used() -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="5 PM London time",
            deadline_date="2026-09-04",
            deadline_time="17:00",
            stated_timezone="Europe/London",
        ),
        request(body_text=f"{BODY} All times are Europe/London."),
    )

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc == datetime(2026, 9, 4, 16, 0, tzinfo=UTC)
    assert resolved.timezone == "Europe/London"


@pytest.mark.parametrize(
    ("zone", "canonical", "at_utc"),
    [
        ("UTC", "UTC", datetime(2026, 9, 4, 17, 0, tzinfo=UTC)),
        ("utc", "UTC", datetime(2026, 9, 4, 17, 0, tzinfo=UTC)),
        ("Etc/UTC", "Etc/UTC", datetime(2026, 9, 4, 17, 0, tzinfo=UTC)),
        ("America/New_York", "America/New_York", datetime(2026, 9, 4, 21, 0, tzinfo=UTC)),
        ("US/Eastern", "US/Eastern", datetime(2026, 9, 4, 21, 0, tzinfo=UTC)),
    ],
)
def test_d6_utc_and_iana_region_zones_resolve_exactly(
    zone: str, canonical: str, at_utc: datetime
) -> None:
    resolved = resolve_deadline(
        friday(deadline_time="17:00", stated_timezone=zone),
        request(body_text=f"{BODY} All times are {zone}."),
    )

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc == at_utc
    assert resolved.timezone == canonical


@pytest.mark.parametrize(
    "zone",
    [
        "EST",
        "MST",
        "CET",
        "GMT",
        "PT",
        "UTC+2",
        "Etc/GMT+5",
        "etc/gmt-3",
        "Eastern Time",
        "Pacific Time",
        "Mars/Olympus_Mons",
    ],
)
def test_d6_abbreviated_offset_and_unknown_zones_keep_only_the_day(zone: str) -> None:
    # The email writes each zone, so the name rules (not the grounding rule) decide.
    resolved = resolve_deadline(
        candidate(
            deadline_text="5 PM PT",
            deadline_date="2026-09-04",
            deadline_time="17:00",
            stated_timezone=zone,
        ),
        request(body_text=f"{BODY} Times are {zone}."),
    )

    assert resolved == ResolvedDeadline(
        "5 PM PT", DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


def resolve_phrase(
    body: str, phrase: str, *, stated_timezone: str | None = None, owner_zone: str = ZONE
) -> ResolvedDeadline:
    """Resolve ``phrase`` due 4 September at 17:00 from an email whose body is ``body``."""
    return resolve_deadline(
        candidate(
            deadline_text=phrase,
            deadline_date="2026-09-04",
            deadline_time="17:00",
            stated_timezone=stated_timezone,
        ),
        request(body_text=body, timezone_name=owner_zone),
    )


FRIDAY = date(2026, 9, 4)
FIVE_PM_TORONTO = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("owner", "body", "phrase", "expected"),
    [
        (
            "UTC",
            "Send it Friday 5pm UTC.",
            "Friday 5pm UTC",
            ResolvedDeadline(
                "Friday 5pm UTC",
                DeadlinePrecision.DATETIME,
                FRIDAY,
                datetime(2026, 9, 4, 17, 0, tzinfo=UTC),
                "UTC",
            ),
        ),
        (
            ZONE,
            "Send it by 5pm on Friday. All times are America/Toronto.",
            "by 5pm",
            ResolvedDeadline("by 5pm", DeadlinePrecision.DATETIME, FRIDAY, FIVE_PM_TORONTO, ZONE),
        ),
        (
            ZONE,
            "Send it by 5pm on Friday.",
            "by 5pm",
            ResolvedDeadline("by 5pm", DeadlinePrecision.DATETIME, FRIDAY, FIVE_PM_TORONTO, ZONE),
        ),
        (
            ZONE,
            "Send it by 5pm ET on Friday.",
            "5pm ET",
            ResolvedDeadline("5pm ET", DeadlinePrecision.DATE, FRIDAY, None, ZONE),
        ),
    ],
    ids=["owner-utc-written", "owner-zone-written", "owner-zone-echoed", "echoed-with-et"],
)
def test_d6_an_owner_zone_the_email_writes_is_used_and_an_echo_counts_as_none(
    owner: str, body: str, phrase: str, expected: ResolvedDeadline
) -> None:
    resolved = resolve_phrase(body, phrase, stated_timezone=owner, owner_zone=owner)

    assert resolved == expected


def test_d6_a_zone_the_email_never_writes_keeps_only_the_day() -> None:
    resolved = resolve_phrase(
        "Send it Friday at 5 PM.", "Friday at 5 PM", stated_timezone="America/Los_Angeles"
    )

    assert resolved == ResolvedDeadline(
        "Friday at 5 PM", DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


def test_d6_a_zone_the_email_writes_resolves_exactly() -> None:
    resolved = resolve_phrase(
        "Send it by 5pm America/Chicago on Friday.",
        "5pm America/Chicago",
        stated_timezone="America/Chicago",
    )

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc == datetime(2026, 9, 4, 22, 0, tzinfo=UTC)
    assert resolved.timezone == "America/Chicago"


@pytest.mark.parametrize(
    ("body", "phrase", "stated", "precision"),
    [
        ("Send it by 5pm on Friday.", "by 5pm", "America/Toronto", DeadlinePrecision.DATETIME),
        ("Send it by 5pm on Friday.", "by 5pm", "america/toronto", DeadlinePrecision.DATETIME),
        ("Send it by 5pm PT on Friday.", "by 5pm PT", "America/Toronto", DeadlinePrecision.DATE),
        ("Send it by 5pm on Friday.", "by 5pm", "America/Vancouver", DeadlinePrecision.DATE),
    ],
    ids=["echoed", "echoed-lowercase", "echoed-with-pt-phrase", "other-zone-not-written"],
)
def test_d6_an_echoed_owner_zone_counts_as_no_stated_zone(
    body: str, phrase: str, stated: str, precision: DeadlinePrecision
) -> None:
    resolved = resolve_phrase(body, phrase, stated_timezone=stated)

    at_utc = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    exact = precision is DeadlinePrecision.DATETIME
    assert resolved == ResolvedDeadline(
        phrase, precision, date(2026, 9, 4), at_utc if exact else None, ZONE
    )


@pytest.mark.parametrize(
    ("body", "phrase"),
    [
        ("Please send it by Friday 5pm PT.", "by Friday 5pm PT"),
        ("请在北京时间下午五点前回复。", "北京时间下午五点"),
        ("SUBMIT IT BY 5PM FRIDAY.", "SUBMIT IT BY 5PM"),  # A documented false positive.
    ],
    ids=["pt", "beijing", "false-positive"],
)
def test_d6_a_phrase_naming_an_unreported_zone_keeps_only_the_day(body: str, phrase: str) -> None:
    resolved = resolve_phrase(body, phrase)

    assert resolved == ResolvedDeadline(
        phrase, DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


def test_d6_a_phrase_without_a_zone_uses_the_owner_zone() -> None:
    resolved = resolve_phrase("Please send it by 5pm on Friday.", "by 5pm")

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc == datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    assert resolved.timezone == ZONE


@pytest.mark.parametrize(
    "phrase",
    [
        "5 PM ET",
        "5pm PT",
        "17:00 PST",
        "noon EDT",
        "9:00 CEST",
        "10am AEST",
        "5pm UTC",
        "17:00 utc",
        "5pm GMT+8",
        "17:00 UTC-05:00",
        "5pm Eastern",
        "5 pm pacific time",
        "Central time",
        "北京时间下午五点",
        "东八区17点",
    ],
)
def test_zone_tokens_are_recognized(phrase: str) -> None:
    assert ZONE_TOKEN_PATTERN.search(phrase) is not None


@pytest.mark.parametrize(
    "phrase",
    ["by 5pm", "Friday 5 PM", "at 17:00", "end of day", "tomorrow at noon", "下午五点"],
)
def test_phrases_without_a_zone_have_no_zone_token(phrase: str) -> None:
    assert ZONE_TOKEN_PATTERN.search(phrase) is None


def test_d7_a_time_in_the_spring_forward_gap_stays_consistent() -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="8 March at 2:30", deadline_date="2026-03-08", deadline_time="02:30"
        ),
        request(datetime(2026, 3, 6, 15, 0, tzinfo=UTC)),
    )

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc is not None
    assert resolved.at_utc == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert resolved.at_utc.astimezone(ZoneInfo(ZONE)).date() == resolved.date == date(2026, 3, 8)


def test_d7_an_ambiguous_fall_back_time_uses_its_first_occurrence() -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="1 November at 1:30", deadline_date="2026-11-01", deadline_time="01:30"
        ),
        request(datetime(2026, 10, 30, 15, 0, tzinfo=UTC)),
    )

    assert resolved.at_utc == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert resolved.date == date(2026, 11, 1)


def test_invalid_deadline_errors_are_static_value_errors() -> None:
    with pytest.raises(ValueError) as caught:
        resolve_deadline(candidate(deadline_text="not in the email"), request())

    assert isinstance(caught.value, InvalidDeadlineError)
    assert "not in the email" not in str(caught.value)
