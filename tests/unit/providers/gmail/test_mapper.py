"""Metadata normalization with no MIME body access."""

import pytest

from mailbrief.domain.messages import MessageImportance
from mailbrief.ports.errors import ProviderResponseError
from mailbrief.providers.gmail.mapper import map_metadata
from tests.unit.providers.gmail.metadata_fixtures import ACCOUNT, NOW, metadata


def test_normalized_metadata_and_account_scoped_link() -> None:
    item = map_metadata(metadata(), ACCOUNT)
    assert item.received_at_utc == NOW
    assert item.importance is MessageImportance.HIGH
    assert not item.is_read
    assert item.is_in_inbox
    assert not item.has_attachments
    assert item.body_preview == "Please approve & reply."
    assert "authuser=me%40example.com" in str(item.web_link)
    assert str(item.web_link).endswith("#all/thread_a1")


def test_omitted_empty_labels_are_not_current_inbox() -> None:
    raw = metadata()
    del raw["labelIds"]
    assert not map_metadata(raw, ACCOUNT).is_in_inbox


def test_malformed_optional_headers_and_body_ignored() -> None:
    raw = metadata()
    raw["payload"] = {
        "headers": [
            {"name": "SUBJECT", "value": "=?utf-8?b?SGVsbG8=?=\n\x1b"},
            {"name": "From", "value": "invalid"},
            {"name": "To", "value": "bad, Good <good@example.com>"},
            {"name": None, "value": []},
        ],
        "body": {"data": "FULL_BODY_MUST_NOT_BE_USED"},
    }
    raw["snippet"] = "x" * 5000
    item = map_metadata(raw, ACCOUNT)
    assert item.subject == "Hello"
    assert item.sender.address == "unknown@invalid"
    assert item.to_recipients[0].address == "good@example.com"
    assert len(item.body_preview) == 2048
    assert "FULL_BODY" not in item.model_dump_json()


@pytest.mark.parametrize(
    "key,value",
    [
        ("internalDate", "not-time"),
        ("internalDate", "9" * 100),
        ("id", "../other"),
        ("threadId", None),
        ("labelIds", "INBOX"),
        ("payload", []),
        ("snippet", 3),
    ],
)
def test_invalid_required_metadata_is_safe(key: str, value: object) -> None:
    raw = metadata()
    raw[key] = value
    with pytest.raises(ProviderResponseError) as error:
        map_metadata(raw, ACCOUNT)
    assert "sender@example.com" not in str(error.value)
