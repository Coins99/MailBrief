"""In-memory message body model: consistency and leak resistance."""

import pytest
from pydantic import ValidationError

from mailbrief.domain.bodies import (
    MAX_ANALYSIS_CHARS,
    MAX_EXTRACTED_CHARS,
    BodySource,
    BodyStatus,
    MessageBody,
    PreparedBody,
)


def test_text_and_source_must_agree() -> None:
    assert MessageBody(provider_message_id="m1", source=BodySource.NONE).text == ""
    with pytest.raises(ValidationError):
        MessageBody(provider_message_id="m1", text="hello", source=BodySource.NONE)
    with pytest.raises(ValidationError):
        MessageBody(provider_message_id="m1", text="   ", source=BodySource.PLAIN)


def test_body_text_never_appears_in_repr_or_errors() -> None:
    body = MessageBody(provider_message_id="m1", text="SECRET-BODY-MARKER", source=BodySource.PLAIN)
    assert "SECRET-BODY-MARKER" not in repr(body)
    with pytest.raises(ValidationError) as error:
        MessageBody(
            provider_message_id="m1",
            text="SECRET-BODY-MARKER" * (MAX_EXTRACTED_CHARS // 10),
            source=BodySource.PLAIN,
        )
    assert "SECRET-BODY-MARKER" not in str(error.value)


def test_prepared_text_exists_only_when_ready() -> None:
    ready = PreparedBody(provider_message_id="m1", status=BodyStatus.READY, text="Hello")
    assert ready.text == "Hello"
    assert PreparedBody(provider_message_id="m1", status=BodyStatus.FAILED).text == ""
    with pytest.raises(ValidationError):
        PreparedBody(provider_message_id="m1", status=BodyStatus.READY)
    with pytest.raises(ValidationError):
        PreparedBody(provider_message_id="m1", status=BodyStatus.EMPTY, text="text")


def test_prepared_text_is_bounded_and_hidden() -> None:
    body = PreparedBody(
        provider_message_id="m1", status=BodyStatus.READY, text="SECRET-PREPARED-MARKER"
    )
    assert "SECRET-PREPARED-MARKER" not in repr(body)
    with pytest.raises(ValidationError) as error:
        PreparedBody(
            provider_message_id="m1",
            status=BodyStatus.READY,
            text="SECRET-PREPARED-MARKER" * MAX_ANALYSIS_CHARS,
        )
    assert "SECRET-PREPARED-MARKER" not in str(error.value)
