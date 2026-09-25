"""Manual review remains deterministic, bounded and account/window scoped."""

from datetime import UTC, datetime

import pytest

from mailbrief.services.ranking import rank_messages, review_shortlist
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
