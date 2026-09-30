"""Manual review remains deterministic, bounded and account/window scoped."""

from datetime import UTC, datetime, timedelta

import pytest

from mailbrief.domain.messages import RankedMessage
from mailbrief.services.ranking import (
    ExcludedSenderError,
    ShortlistReviewError,
    rank_messages,
    review_shortlist,
)
from tests.factories import make_message


def test_manual_inclusion_and_exclusion() -> None:
    ranked = rank_messages(
        [make_message(provider_message_id=f"message-{i}") for i in range(15)],
        user_email="me@example.com",
        now_utc=datetime(2026, 9, 25, tzinfo=UTC),
    )
    selected = review_shortlist(ranked, include_ids=("message-9",), exclude_ids=("message-0",))
    ids = [item.message.provider_message_id for item in selected]
    assert "message-9" in ids and "message-0" not in ids
    assert len(ids) <= 10
    assert selected == review_shortlist(
        list(reversed(ranked)), include_ids=("message-9",), exclude_ids=("message-0",)
    )


@pytest.mark.parametrize("included,excluded", [(("absent",), ()), (("message-0",), ("message-0",))])
def test_invalid_review_ids(included: tuple[str, ...], excluded: tuple[str, ...]) -> None:
    ranked = rank_messages(
        [make_message(provider_message_id="message-0")],
        user_email="me@example.com",
        now_utc=datetime(2026, 9, 25, tzinfo=UTC),
    )
    with pytest.raises(ValueError):
        review_shortlist(ranked, include_ids=included, exclude_ids=excluded)


def ranked_inbox(count: int) -> list[RankedMessage]:
    """``count`` messages that all qualify, newest first: message-0 ranks highest."""
    return rank_messages(
        [
            make_message(
                provider_message_id=f"message-{index}",
                received_at_utc=datetime(2026, 9, 25, 12, tzinfo=UTC) - timedelta(minutes=index),
            )
            for index in range(count)
        ],
        user_email="me@example.com",
        now_utc=datetime(2026, 9, 25, 13, tzinfo=UTC),
    )


def ids(selected: list[RankedMessage]) -> list[str]:
    return [item.message.provider_message_id for item in selected]


def test_blocked_messages_never_take_a_slot() -> None:
    ranked = ranked_inbox(12)
    blocked = frozenset({"message-0", "message-3"})
    selected = ids(review_shortlist(ranked, blocked=blocked))
    assert len(selected) == 10
    assert not blocked & set(selected)
    assert "message-10" in selected and "message-11" in selected  # Backfilled.


def test_per_run_exclusions_are_not_backfilled() -> None:
    selected = ids(review_shortlist(ranked_inbox(12), exclude_ids=("message-0",)))
    assert len(selected) == 9
    assert "message-10" not in selected


def test_a_blocked_message_can_t_be_included() -> None:
    with pytest.raises(ExcludedSenderError, match="excluded sender") as caught:
        review_shortlist(
            ranked_inbox(3), include_ids=("message-1",), blocked=frozenset({"message-1"})
        )
    assert isinstance(caught.value, ShortlistReviewError)
    assert "message-1" not in str(caught.value)


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 10])
def test_the_limit_caps_the_automatic_selection(limit: int) -> None:
    selected = ids(review_shortlist(ranked_inbox(12), limit=limit))
    assert selected == [f"message-{index}" for index in range(limit)]


@pytest.mark.parametrize("limit", [1, 2])
def test_a_limit_below_three_backfills_only_to_the_limit(limit: int) -> None:
    quiet = [
        item.model_copy(update={"score": 0}) for item in ranked_inbox(5)
    ]  # Nothing qualifies, so the selection is backfilled.
    assert len(review_shortlist(quiet, limit=limit)) == limit


def test_the_limit_caps_inclusions() -> None:
    ranked = ranked_inbox(12)
    selected = ids(review_shortlist(ranked, include_ids=("message-11",), limit=2))
    assert selected == ["message-0", "message-11"]
    with pytest.raises(ShortlistReviewError, match="Too many"):
        review_shortlist(ranked, include_ids=("message-10", "message-11"), limit=1)


@pytest.mark.parametrize("limit", [0, 11])
def test_the_limit_must_be_one_to_ten(limit: int) -> None:
    with pytest.raises(ValueError, match="1 to 10"):
        review_shortlist(ranked_inbox(1), limit=limit)
