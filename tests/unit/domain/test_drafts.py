"""Draft rules: kinds and recipients, limits, placeholders, titles, exports and filenames."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from mailbrief.domain.drafts import (
    DRAFT_BODY_MAX_CHARS,
    DRAFT_RECIPIENTS_MAX_CHARS,
    DRAFT_TITLE_MAX_CHARS,
    Draft,
    DraftEdit,
    DraftKind,
    DraftSource,
    DraftSummary,
    DraftVersion,
    DraftVersionInfo,
    DraftVersionOrigin,
    export_filename,
    export_markdown,
    export_text,
    first_line,
    placeholders,
    recipient_warnings,
)

AT = datetime(2026, 9, 28, 13, tzinfo=UTC)


def draft(**overrides: Any) -> Draft:
    values: dict[str, Any] = {
        "public_id": "00000000-0000-4000-8000-000000000001",
        "kind": DraftKind.NOTE,
        "created_at_utc": AT,
        "updated_at_utc": AT,
        "revision": 1,
    }
    values.update(overrides)
    return Draft.model_validate(values)


@pytest.mark.parametrize("kind", [DraftKind.REPLY, DraftKind.EMAIL])
def test_replies_and_emails_may_have_recipients(kind: DraftKind) -> None:
    made = draft(kind=kind, to_text="alex@example.com", cc_text="sam@")
    assert (made.to_text, made.cc_text) == ("alex@example.com", "sam@")


@pytest.mark.parametrize("kind", [DraftKind.NOTE, DraftKind.MESSAGE])
@pytest.mark.parametrize("field", ["to_text", "cc_text"])
def test_notes_and_messages_may_not_have_recipients(kind: DraftKind, field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        draft(kind=kind, **{field: "private@example.com"})
    assert "private@example.com" not in str(caught.value)
    assert draft(kind=kind, **{field: "  "}).kind is kind  # Blank is no recipient.


def test_text_is_kept_exactly_as_typed() -> None:
    made = draft(title="  Plan ", body="\n  indented\n\n")
    assert (made.title, made.body) == ("  Plan ", "\n  indented\n\n")
    edit = DraftEdit(body=" x ")
    assert edit.body == " x "


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("title", DRAFT_TITLE_MAX_CHARS),
        ("to_text", DRAFT_RECIPIENTS_MAX_CHARS),
        ("cc_text", DRAFT_RECIPIENTS_MAX_CHARS),
        ("body", DRAFT_BODY_MAX_CHARS),
    ],
)
def test_limits(field: str, limit: int) -> None:
    assert len(getattr(DraftEdit(**{field: "a" * limit}), field)) == limit
    with pytest.raises(ValidationError) as caught:
        DraftEdit(**{field: "secret" + "a" * limit})
    assert "secret" not in str(caught.value)
    assert (DRAFT_TITLE_MAX_CHARS, DRAFT_RECIPIENTS_MAX_CHARS, DRAFT_BODY_MAX_CHARS) == (
        200,
        2_000,
        20_000,
    )


def test_text_fields_stay_out_of_repr() -> None:
    made = draft(title="private title", body="private body")
    assert "private" not in repr(made)
    assert "private" not in repr(DraftEdit(body="private"))


def test_a_draft_needs_a_positive_revision_and_aware_times() -> None:
    with pytest.raises(ValidationError):
        draft(revision=0)
    with pytest.raises(ValidationError):
        draft(created_at_utc=datetime(2026, 9, 28))  # noqa: DTZ001 - naive on purpose.
    toronto = timezone(timedelta(hours=-4))
    assert draft(updated_at_utc=AT.astimezone(toronto)).updated_at_utc.tzinfo is UTC


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ()),
        ("alex@example.com", ()),
        ("alex@example.com, sam@example.com; kim@example.com", ()),
        ("Alex <alex@example.com>", ()),
        ("alex@example.com,, ;", ()),
        ("alex", ("alex",)),
        ("alex@, @example.com", ("alex@", "@example.com")),
        ("a@b@c; ok@example.com", ("a@b@c",)),
        (" sam  ; kim@x ", ("sam",)),
    ],
)
def test_recipient_warnings(text: str, expected: tuple[str, ...]) -> None:
    assert recipient_warnings(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Dear [[name]], see [[date]].", ("[[name]]", "[[date]]")),
        ("[[name]] and [[name]] again", ("[[name]]",)),
        ("[[ ]]", ("[[ ]]",)),
        ("[[]] [[a[b]] [[a]b]] [[split\nline]]", ()),
        ("[[" + "x" * 60 + "]]", ("[[" + "x" * 60 + "]]",)),
        ("[[" + "x" * 61 + "]]", ()),
        ("[[[inner]]]", ("[[inner]]",)),
        ("single [brackets] only", ()),
    ],
)
def test_placeholders(text: str, expected: tuple[str, ...]) -> None:
    assert placeholders(text) == expected


def test_a_drafts_placeholders_cover_recipients_title_and_body_in_order() -> None:
    made = draft(
        kind=DraftKind.EMAIL,
        to_text="[[to]]",
        cc_text="[[cc]]",
        title="About [[topic]]",
        body="Hi [[to]], [[when]]",
    )
    assert made.placeholders == ("[[to]]", "[[cc]]", "[[topic]]", "[[when]]")
    # A token never spans two fields.
    assert draft(title="[[half", body="of]]").placeholders == ()


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"title": "  Budget  ", "body": "ignored"}, "Budget"),
        ({"body": "\n  \n  First line  \nsecond"}, "First line"),
        ({"body": "x" * 90}, "x" * 80),
        ({"body": "   "}, "Untitled note"),
        ({"kind": DraftKind.MESSAGE}, "Untitled message"),
        ({"kind": DraftKind.REPLY}, "Untitled reply"),
    ],
)
def test_display_title(fields: dict[str, Any], expected: str) -> None:
    assert draft(**fields).display_title == expected


def test_first_line_trims_after_cutting() -> None:
    assert first_line("abc   def", limit=5) == "abc"
    assert first_line("") == ""


def test_content_is_the_text_alone() -> None:
    made = draft(kind=DraftKind.EMAIL, title="T", to_text="a@b", cc_text="c@d", body="B")
    assert made.content() == DraftEdit(title="T", to_text="a@b", cc_text="c@d", body="B")


def test_source_and_listing_models() -> None:
    source = DraftSource(
        provider_message_id="m-1",
        subject="Hello",
        sender_address="alex@example.com",
        web_link="https://mail.google.com/mail/u/0/#all/m-1",
        received_at_utc=AT,
        available=False,
    )
    assert "alex@example.com" not in repr(source)
    summary = DraftSummary(
        public_id="00000000-0000-4000-8000-000000000001",
        kind=DraftKind.NOTE,
        display_title="Plan",
        updated_at_utc=AT,
        placeholder_count=0,
    )
    assert summary.action_title is None
    info = DraftVersionInfo(
        number=1, origin=DraftVersionOrigin.CREATED, created_at_utc=AT, preview="", length=0
    )
    assert info.origin is DraftVersionOrigin.CREATED
    with pytest.raises(ValidationError):
        DraftVersionInfo(
            number=0, origin=DraftVersionOrigin.EDITED, created_at_utc=AT, preview="", length=0
        )
    version = DraftVersion(number=2, origin="restored", body=" kept ", created_at_utc=AT)
    assert version.body == " kept "


EMAIL = draft(
    kind=DraftKind.REPLY,
    title="Re: Budget",
    to_text=" alex@example.com ",
    cc_text="sam@example.com",
    body="Hi Alex,\n\nThanks.",
)


def test_email_exports() -> None:
    assert export_text(EMAIL) == (
        "To: alex@example.com\nCc: sam@example.com\nSubject: Re: Budget\n\nHi Alex,\n\nThanks.\n"
    )
    assert export_markdown(EMAIL) == (
        "**To:** alex@example.com  \n**Cc:** sam@example.com  \n**Subject:** Re: Budget\n\n"
        "Hi Alex,\n\nThanks.\n"
    )


def test_an_empty_email_still_names_its_headers() -> None:
    empty = draft(kind=DraftKind.EMAIL)
    assert export_text(empty) == "To:\nSubject:\n\n"
    assert export_markdown(empty) == "**To:**  \n**Subject:**\n\n"


def test_note_and_message_exports() -> None:
    note = draft(title="Plan", body="- one\n- two\n")
    assert export_text(note) == "Plan\n\n- one\n- two\n"
    assert export_markdown(note) == "# Plan\n\n- one\n- two\n"
    message = draft(kind=DraftKind.MESSAGE, body="On my way")
    assert export_text(message) == "On my way\n"
    assert export_markdown(message) == "On my way\n"
    assert export_text(draft()) == ""


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"title": "Re: Budget / Q3?"}, "Re Budget Q3.md"),
        ({"title": "Café plan_v2.final"}, "Café plan_v2.final.md"),
        ({"title": "a" * 100}, "a" * 80 + ".md"),
        ({"title": "★ ☆ ✓"}, "mailbrief-draft.md"),
        ({"title": "...hidden..."}, "hidden.md"),
        ({"title": "con"}, "mailbrief-draft.md"),
        ({"title": "LPT1.backup"}, "mailbrief-draft.md"),
        ({}, "Untitled note.md"),
    ],
)
def test_export_filename(fields: dict[str, Any], expected: str) -> None:
    assert export_filename(draft(**fields), ".md") == expected


def test_export_filename_uses_the_suffix() -> None:
    assert export_filename(draft(title="Plan"), ".txt") == "Plan.txt"
