"""Plain-text labels, escaped button text and the elided coverage line."""

import math

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mailbrief.ui.labels import (
    ElidedLabel,
    WrapLabel,
    button_label,
    cut_text,
    plain_label,
    short_button_label,
    wrap_label,
)
from mailbrief.ui.theme import CAPTION_PX, SIDEBAR_WIDTH, apply_theme
from mailbrief.ui.workspace import SidebarNav

HOSTILE = '<b>Win</b> & <a href="x">link</a>'
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"


def test_plain_label_is_plain_wrapped_text(qtbot: QtBot) -> None:
    label = plain_label(HOSTILE, tone="muted", px=11, medium=True)
    qtbot.addWidget(label)
    assert label.textFormat() is Qt.TextFormat.PlainText
    assert label.wordWrap() and label.text() == HOSTILE
    assert label.property("tone") == "muted"
    assert label.font().pixelSize() == 11
    assert plain_label().text() == ""


def test_button_label_escapes_mnemonics() -> None:
    assert button_label("Q&A & more") == "Q&&A && more"


def test_elided_label_never_drives_width(qtbot: QtBot) -> None:
    label = ElidedLabel(HOSTILE + " " * 3 + "x" * 200, tone="muted", px=11)
    qtbot.addWidget(label)
    assert label.textFormat() is Qt.TextFormat.PlainText and not label.wordWrap()
    assert label.sizeHint().width() == 0 and label.minimumSizeHint().width() == 0
    assert label.accessibleName() == label.text()
    assert label.toolTip() == ""
    label.resize(120, 20)
    assert not label.grab().isNull()
    label.setText("Covers today")
    assert label.accessibleName() == "Covers today"


def test_short_button_label_cuts_long_text_to_one_escaped_line() -> None:
    assert short_button_label("Add to\n  “Plan”") == "Add to “Plan”"
    long = "A & B " * 20
    short = short_button_label(long)
    assert len(short.replace("&&", "&")) == 40 and short.endswith("…")
    assert "&&" in short
    assert short_button_label("x" * 40) == "x" * 40
    assert short_button_label("x" * 12, limit=10) == "x" * 9 + "…"


def test_elided_label_takes_a_separate_accessible_name(qtbot: QtBot) -> None:
    label = ElidedLabel()
    qtbot.addWidget(label)
    label.setText("Inbox on Oct 6, up to 09:14", "Covers messages received on 2026-10-06 …")
    assert label.text() == "Inbox on Oct 6, up to 09:14"
    assert label.accessibleName() == "Covers messages received on 2026-10-06 …"


TOKEN = "x" * 300  # An unbroken token, such as a long address or link.


def test_wrap_label_wraps_an_unbroken_token(qtbot: QtBot, qapp: QApplication, themed: None) -> None:
    apply_theme(qapp)
    label = wrap_label(TOKEN, tone="secondary", px=13, medium=True)
    qtbot.addWidget(label)
    assert isinstance(label, WrapLabel)
    assert label.textFormat() is Qt.TextFormat.PlainText
    assert label.property("tone") == "secondary"
    assert label.font().pixelSize() == 13
    assert label.hasHeightForWidth()
    one_line = label.minimumSizeHint().height()
    assert label.minimumSizeHint().width() == 0
    assert label.heightForWidth(120) > one_line
    assert label.text() == TOKEN and label.accessibleName() == TOKEN
    assert label.toolTip() == ""
    limit = QFontMetricsF(label.font()).averageCharWidth() * 40
    hint = label.sizeHint()
    assert hint.width() <= math.ceil(limit)
    assert hint.height() == label.heightForWidth(hint.width())
    label.resize(120, label.heightForWidth(120))
    assert not label.grab().isNull()


def test_wrap_label_height_includes_its_margins(qtbot: QtBot) -> None:
    label = wrap_label("short")
    qtbot.addWidget(label)
    plain = label.heightForWidth(200)
    label.setContentsMargins(10, 3, 4, 5)
    assert label.heightForWidth(214) == plain + 8
    assert label.minimumSizeHint().height() == plain + 8


def test_wrap_label_keeps_line_breaks_and_follows_its_text(qtbot: QtBot) -> None:
    label = wrap_label("one")
    qtbot.addWidget(label)
    one = label.heightForWidth(400)
    label.setText("one\ntwo")
    assert label.heightForWidth(400) > one
    assert label.accessibleName() == "one\ntwo"


def laid_out_lines(label: WrapLabel, width: int) -> list[str]:
    """The lines ``label`` lays out at ``width``, without the soft breaks it adds."""
    layout, _height, _widest = label._layout(width)
    text = layout.text()
    return [
        text[line.textStart() : line.textStart() + line.textLength()].replace("​", "")
        for line in (layout.lineAt(number) for number in range(layout.lineCount()))
    ]


def test_an_address_wraps_after_its_at_sign(qtbot: QtBot, qapp: QApplication, themed: None) -> None:
    apply_theme(qapp)
    sidebar = SidebarNav()
    qtbot.addWidget(sidebar)
    sidebar_layout = sidebar.layout()
    assert sidebar_layout is not None
    margins = sidebar_layout.contentsMargins()
    footer_width = SIDEBAR_WIDTH - margins.left() - margins.right()
    line = wrap_label("Gmail: connected as owner@example.com", px=CAPTION_PX)
    qtbot.addWidget(line)
    line.setContentsMargins(10, 0, 0, 0)  # As in the window's sidebar footer.
    lines = laid_out_lines(line, footer_width - 10)
    assert "".join(lines).replace(" ", "") == "Gmail:connectedasowner@example.com"
    assert any(text.rstrip().endswith("@") for text in lines)  # It breaks after the @...
    assert any("example.com" in text for text in lines)  # ...never inside the domain.
    assert "​" not in line.text() and "​" not in line.accessibleName()


def test_a_path_wraps_after_its_slashes(qtbot: QtBot) -> None:
    label = wrap_label("https://mail.google.com/mail/u/0/#inbox/abc")
    qtbot.addWidget(label)
    lines = laid_out_lines(label, 120)
    assert len(lines) > 1
    assert all(not text or text.endswith("/") for text in lines[:-1])
    assert "​" not in label.text()


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("a" * 50, 40, "a" * 39 + "…"),
        ("x" * 36 + FAMILY + "tail", 40, "x" * 36 + "…"),  # A ZWJ sequence across the cut.
        ("y" * 38 + "\U0001f1e8\U0001f1e6" + "zz", 40, "y" * 38 + "…"),  # A flag.
        ("z" * 38 + "e\u0301" + "more", 40, "z" * 38 + "…"),  # A letter and its accent.
        ("\U0001f600" * 30, 24, "\U0001f600" * 23 + "…"),  # Two UTF-16 units each.
        ("short", 40, "short"),
        ("x" * 40, 40, "x" * 40),
    ],
)
def test_cut_text_cuts_at_a_grapheme_boundary(text: str, limit: int, expected: str) -> None:
    result = cut_text(text, limit)
    assert result == expected
    assert len(result) <= limit
    assert "\u200d" not in result or "\u200d" in expected


def test_cut_text_needs_room_for_the_ellipsis() -> None:
    with pytest.raises(ValueError):
        cut_text("anything", 0)


def test_short_button_label_never_splits_an_emoji() -> None:
    short = short_button_label("Q&A " + "x" * 32 + FAMILY + " tail")
    assert short == "Q&&A " + "x" * 32 + "…"
    assert "\u200d" not in short
