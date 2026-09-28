"""Tolerant quote matching: case, width, quotes and whitespace."""

import pytest

from mailbrief.text.matching import appears_in, copies_long_run


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


def test_a_copied_run_is_found_only_at_the_limit() -> None:
    source = "".join(chr(ord("a") + (index * 7) % 26) for index in range(400))

    assert copies_long_run("Intro. " + source[50:250] + " Outro.", source, 200)
    assert not copies_long_run("Intro. " + source[50:249] + " Outro.", source, 200)
    assert copies_long_run(source[:10], source, 10)


def test_normalization_does_not_hide_a_copy() -> None:
    source = "Please send the “final” numbers by Friday.   Thanks, Alex"
    copied = 'PLEASE SEND THE "FINAL" NUMBERS\nBY FRIDAY. THANKS, ALEX'
    fullwidth = "Ｐｌｅａｓｅ send the"

    assert copies_long_run(copied, source, len('please send the "final" numbers by friday.'))
    assert copies_long_run(fullwidth, source, len("please send the"))
    assert not copies_long_run("Please send numbers", source, 16)


def test_short_texts_and_bad_limits() -> None:
    assert not copies_long_run("short", "short", 200)
    assert not copies_long_run("x" * 300, "", 200)
    with pytest.raises(ValueError):
        copies_long_run("a", "a", 0)
