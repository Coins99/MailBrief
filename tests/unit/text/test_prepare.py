"""Text preparation: cleanup, quoted-history trimming, forwards and length limits."""

import pytest

from mailbrief.text.prepare import (
    looks_like_forward,
    normalize_text,
    trim_quoted_history,
    truncate_at_boundary,
)


def test_normalize_removes_invisible_and_control_characters() -> None:
    raw = "Hi\u200b there\r\n\r\n\r\n\r\nLine\x07 two  \n\tIndented\ufeff"
    assert normalize_text(raw) == "Hi there\n\nLine two\n\tIndented"


@pytest.mark.parametrize(
    "text",
    [
        "Sounds good, see you then.\n\n"
        "On Mon, Sep 21, 2026 at 9:00 AM Alex <alex@example.com> wrote:\n"
        "> Can we meet at 3?\n> Thanks",
        "Sounds good, see you then.\n\n"
        "Alex <alex@example.com> 于2026年9月21日周一 09:00写道：\n> 三点见？",
        "Sounds good, see you then.\n\n"
        "On Mon, Sep 21, 2026 at 9:00 AM Alex wrote:\n\n> Can we meet?\n> > Earlier note",
        "Sounds good, see you then.\n\n-----Original Message-----\nFrom: Alex\nSent: Monday\n"
        "Subject: Meeting\n\nCan we meet at 3?",
        "Sounds good, see you then.\n\n________________________________\nFrom: Alex\nSent: Monday\n"
        "Can we meet at 3?",
        "Sounds good, see you then.\n\nFrom: Alex <alex@example.com>\nSent: Monday, 21 September\n"
        "To: Me\nSubject: Meeting\n\nCan we meet at 3?",
    ],
    ids=["english", "chinese", "html-derived", "outlook-original", "outlook-line", "outlook-block"],
)
def test_reply_history_is_trimmed(text: str) -> None:
    assert trim_quoted_history(text) == ("Sounds good, see you then.", True)


def test_forwards_are_never_trimmed() -> None:
    gmail = "FYI\n\n---------- Forwarded message ---------\nFrom: Pat\n\n> quoted inside"
    apple = "FYI\n\nBegin forwarded message:\n\nFrom: Pat\nDate: Monday\n\nBudget"
    assert trim_quoted_history(gmail) == (gmail, False)
    assert trim_quoted_history(apple) == (apple, False)
    outlook = "FYI\n\n-----Original Message-----\nFrom: Pat\nSent: Monday\n\nBudget"
    assert trim_quoted_history(outlook, is_forward=True) == (outlook, False)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Fwd: Budget", True),
        ("FW: Budget", True),
        ("WG: Budget", True),
        ("转发：预算", True),
        ("Re: Budget", False),
        ("Forward planning", False),
        (None, False),
    ],
)
def test_forward_subjects(subject: str | None, expected: bool) -> None:
    assert looks_like_forward(subject) is expected


def test_inline_replies_keep_their_answers() -> None:
    text = "> Can you make Friday?\nYes, Friday works.\n> And the budget?\nApproved."
    assert trim_quoted_history(text) == (text, False)


def test_interleaved_answer_above_the_last_quote_is_kept() -> None:
    text = "> Can you send the file?\nSure, details below:\n> Deadline and budget?"
    assert trim_quoted_history(text) == ("> Can you send the file?\nSure, details below:", True)


@pytest.mark.parametrize(
    "text",
    [
        "Your transfer is complete.\n\nFrom: Everyday Checking\nTo: Savings\n"
        "Date: 25 September 2026\nAmount: $500.00",
        "Booking confirmed.\n\nFrom: London (LHR)\nTo: New York (JFK)\nDate: 3 October 2026",
        "Three things this week.\n\n1. Rent is due.\n\n____________________\n\n2. Bins go out.",
    ],
    ids=["transfer", "itinerary", "divider"],
)
def test_header_like_content_is_kept(text: str) -> None:
    assert trim_quoted_history(text) == (text, False)


def test_entirely_quoted_message_is_kept() -> None:
    text = "On Monday Alex wrote:\n> Only quoted content"
    assert trim_quoted_history(text) == (text, False)


def test_text_without_history_is_unchanged() -> None:
    text = "Please approve the budget by Friday.\nThanks: Sam"
    assert trim_quoted_history(text) == (text, False)


def test_truncation_prefers_line_then_word_boundaries() -> None:
    assert truncate_at_boundary("short", 10) == ("short", False)
    assert truncate_at_boundary("x" * 10, 10) == ("x" * 10, False)
    assert truncate_at_boundary("aaaaaaaaa\nbbbbbbbbbb", 12) == ("aaaaaaaaa", True)
    assert truncate_at_boundary("aaaaaaaaa bbbbbbbbbb", 12) == ("aaaaaaaaa", True)
    assert truncate_at_boundary("a" * 30, 12) == ("a" * 12, True)
