"""Tests for normalized email and ranking models."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    MessagePage,
    ProviderKind,
    RankedMessage,
    RankReason,
)
from tests.factories import make_message


def test_normalized_message_round_trips_as_json() -> None:
    message = make_message()

    restored = type(message).model_validate_json(message.model_dump_json())

    assert restored == message
    assert restored.received_at_utc.tzinfo is UTC


def test_received_timestamp_is_normalized_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))

    message = make_message(received_at_utc=datetime(2026, 8, 31, 10, 30, tzinfo=eastern))

    assert message.received_at_utc == datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    assert message.received_at_utc.tzinfo is UTC


def test_naive_received_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="time zone"):
        make_message(received_at_utc=datetime(2026, 8, 31, 14, 30))


@pytest.mark.parametrize("address", ["missing-at.example.com", "@example.com", "person@"])
def test_invalid_email_contact_is_rejected(address: str) -> None:
    with pytest.raises(ValidationError, match="email address"):
        EmailContact(address=address)


def test_extra_provider_fields_are_rejected() -> None:
    values = make_message().model_dump(mode="python")
    values["provider_payload"] = {"unexpected": True}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        type(make_message()).model_validate(values)


def test_account_identity_validates_email_address() -> None:
    with pytest.raises(ValidationError, match="email address"):
        AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id="account-1",
            email_address="invalid",
        )


def test_message_page_requires_a_positive_page_number() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        MessagePage(page_number=0, messages=())


def test_ranked_message_rejects_duplicate_reasons() -> None:
    with pytest.raises(ValidationError, match="ranking reasons must be unique"):
        RankedMessage(
            message=make_message(),
            score=20,
            reasons=(RankReason.UNREAD, RankReason.UNREAD),
        )


def test_ranked_message_accepts_auditable_reasons() -> None:
    ranked = RankedMessage(
        message=make_message(),
        score=36,
        reasons=(RankReason.HIGH_IMPORTANCE, RankReason.UNREAD, RankReason.DIRECT_RECIPIENT),
    )

    assert ranked.score == 36
    assert ranked.reasons[-1] is RankReason.DIRECT_RECIPIENT
