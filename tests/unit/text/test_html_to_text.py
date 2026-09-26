"""Standard-library HTML-to-text conversion for untrusted email markup."""

import time

import pytest

from mailbrief.text.html_to_text import MAX_QUOTE_DEPTH, convert_html, html_to_text


def test_blocks_lists_and_tables_become_lines() -> None:
    markup = (
        "<p>Hello <b>team</b>,</p><div>Agenda:<br>first<br/>second</div>"
        "<ul><li>One</li><li>Two</li></ul>"
        "<table><tr><td>Name</td><td>Due</td></tr><tr><td>Report</td><td>Friday</td></tr></table>"
    )
    assert html_to_text(markup) == (
        "Hello team,\n\nAgenda:\nfirst\nsecond\n\n- One\n- Two\n\nName Due\nReport Friday"
    )


def test_scripts_styles_and_head_are_dropped() -> None:
    markup = (
        "<html><head><title>Title</title><style>p {color: red}</style></head>"
        "<body><script>alert(1)</script><p>Visible</p><noscript>fallback</noscript></body></html>"
    )
    assert html_to_text(markup) == "Visible"


def test_hidden_text_is_dropped() -> None:
    markup = (
        '<div style="display: none !important">preheader<div>nested</div></div>'
        '<span style="font-size:0px">tiny</span><p hidden>attribute</p>'
        '<p style="MSO-HIDE: ALL">outlook</p><p style="visibility:hidden">invisible</p>'
        '<p style="font-size:14px; opacity:0.9">Shown</p>'
    )
    assert html_to_text(markup) == "Shown"


def test_links_keep_text_and_images_are_dropped() -> None:
    markup = (
        '<p>Read the <a href="https://track.example/abc?u=1">full report</a> '
        '<img src="https://img.example/p.png" alt="logo">today.</p>'
    )
    text = html_to_text(markup)
    assert text == "Read the full report today."
    assert "track.example" not in text and "logo" not in text


def test_entities_and_invisible_characters() -> None:
    markup = "<p>Tom&nbsp;&amp;&nbsp;Jerry&#8217;s\u200b&zwnj;\ufeff plan&hellip;</p>"
    assert html_to_text(markup) == "Tom & Jerry\u2019s plan\u2026"


def test_blockquotes_become_quoted_lines() -> None:
    markup = (
        "<div>Sounds good.</div>"
        '<div class="gmail_attr">On Mon, Sep 21, 2026 at 9:00 AM Alex wrote:<br></div>'
        '<blockquote class="gmail_quote">Can you send the report?'
        "<blockquote>Older note</blockquote></blockquote>"
    )
    assert html_to_text(markup) == (
        "Sounds good.\n\nOn Mon, Sep 21, 2026 at 9:00 AM Alex wrote:\n\n"
        "> Can you send the report?\n> > Older note"
    )


def test_forwarded_content_is_not_quoted() -> None:
    markup = (
        '<div>FYI</div><div class="gmail_quote">---------- Forwarded message ---------'
        "<br>From: Pat<div>Budget attached.</div></div>"
    )
    text = html_to_text(markup)
    assert "Budget attached." in text
    assert ">" not in text


def test_malformed_markup_is_tolerated() -> None:
    markup = "<div><p>Open para<div>nested</span></b> after</div></p><p>tail"
    assert html_to_text(markup) == "Open para\nnested after\n\ntail"
    assert html_to_text("") == ""


def test_oversized_numeric_references_do_not_fail() -> None:
    markup = "<p>A&#" + "9" * 5_000 + ";B &#" + "0" * 5_000 + "65;</p>"
    assert html_to_text(markup) == "A\ufffdB A"


def test_unmatched_end_tags_stay_fast() -> None:
    markup = "<div>" * 20_000 + "</span>" * 20_000 + "<p>after</p>"
    started = time.perf_counter()
    assert html_to_text(markup) == "after"
    assert time.perf_counter() - started < 2.0


def test_deeply_nested_quotes_cannot_multiply_the_output() -> None:
    markup = "<blockquote>" * 3_000 + "x<br>" * 3_000
    text = html_to_text(markup)
    assert text.splitlines()[0] == "> " * MAX_QUOTE_DEPTH + "x"
    assert len(text) < 3_000 * 2 * (MAX_QUOTE_DEPTH + 1)


def test_conversion_stops_at_the_size_limit_and_says_so() -> None:
    text, cut = convert_html("<p>" + "word " * 100 + "</p>", max_chars=50)
    assert cut and len(text) <= 50
    assert convert_html("<p>short</p>", max_chars=50) == ("short", False)
    assert convert_html("<p>exact</p>   ", max_chars=5) == ("exact", False)


def test_blank_lines_do_not_pile_up() -> None:
    assert html_to_text("<br>" * 100_000 + "<p></p>" * 50_000 + "end") == "end"


@pytest.mark.parametrize(
    "filler",
    [" \n\t" * 100_000, "&zwnj;&nbsp;" * 100_000, "\u034f " * 100_000],
    ids=["whitespace", "entity-padding", "grapheme-joiner-padding"],
)
def test_whitespace_and_padding_do_not_use_up_the_budget(filler: str) -> None:
    markup = "<div>" + filler + "</div><p>Real content</p>"
    assert convert_html(markup, max_chars=1_000) == ("Real content", False)


def test_collapsed_spacing_between_words_is_kept() -> None:
    assert html_to_text("<p>Hello   \n  <b>big</b>\t\tworld</p>") == "Hello big world"
    assert html_to_text("<table><tr><td>Name</td><td>Due</td></tr></table>") == "Name Due"
