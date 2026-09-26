"""Standard-library HTML-to-text conversion for untrusted email markup."""

import time

from mailbrief.text.html_to_text import html_to_text


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
