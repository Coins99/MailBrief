"""In-memory message body model: consistency and leak resistance."""

import pytest
from pydantic import ValidationError

from mailbrief.domain.bodies import MAX_EXTRACTED_CHARS, BodySource, MessageBody


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
