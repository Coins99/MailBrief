"""Tolerant quote matching: case, width, quotes and whitespace."""

import pytest

from mailbrief.text.matching import appears_in


@pytest.mark.parametrize(
    ("fragment", "source"),
    [
        ("APPROVE THE BUDGET", "Please approve the budget today."),
        ("ｂｕｄｇｅｔ １２", "The budget 12 is final."),
        ("the “final” plan", 'Here is the "final" plan.'),
        ('the "final" plan', "Here is the “final” plan."),
        ("it’s due friday", "It's due Friday."),
        ("approve the budget by Friday", "Please approve the\nbudget   by\r\n\tFriday."),
        ("  approve the budget\n", "approve the budget"),
    ],
    ids=[
        "case",
        "nfkc-width",
        "curly-fragment",
        "curly-source",
        "apostrophe",
        "line-breaks",
        "surrounding-space",
    ],
)
def test_normalized_fragments_match(fragment: str, source: str) -> None:
    assert appears_in(fragment, source)


def test_any_source_can_match() -> None:
    assert appears_in("quarterly plan", "An unrelated body.", "Re: Quarterly plan")


@pytest.mark.parametrize("fragment", ["", "   ", "\n\t"])
def test_empty_fragment_never_matches(fragment: str) -> None:
    assert not appears_in(fragment, "Any body text")


def test_absent_fragment_does_not_match() -> None:
    assert not appears_in("approve the invoice", "Please approve the budget.")
    assert not appears_in("budget")
