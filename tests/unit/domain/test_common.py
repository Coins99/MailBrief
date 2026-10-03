"""The small helpers every layer shares: counted nouns and UTC timestamps."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from mailbrief.domain.common import counted, normalize_utc, utc_now


@pytest.mark.parametrize(
    ("count", "noun", "text"),
    [
        (0, "message", "0 messages"),
        (1, "message", "1 message"),
        (2, "message", "2 messages"),
        (1, "new message", "1 new message"),
        (3, "tracked thread", "3 tracked threads"),
    ],
)
def test_counted_is_plural_unless_the_count_is_one(count: int, noun: str, text: str) -> None:
    assert counted(count, noun) == text


def test_utc_now_is_aware_and_in_utc() -> None:
    before = datetime.now(UTC)
    now = utc_now()
    assert now.tzinfo is UTC
    assert before <= now <= datetime.now(UTC)


def test_normalize_utc_converts_aware_times_and_refuses_naive_ones() -> None:
    toronto = timezone(timedelta(hours=-4))
    assert normalize_utc(datetime(2026, 10, 3, 8, tzinfo=toronto)) == datetime(
        2026, 10, 3, 12, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="time zone"):
        normalize_utc(datetime(2026, 10, 3, 8))  # noqa: DTZ001 - naive on purpose.
