"""Plain-text labels, escaped button text and the elided coverage line."""

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mailbrief.ui.labels import (
    ElidedLabel,
    WrapLabel,
    button_label,
    plain_label,
    short_button_label,
    wrap_label,
)
from mailbrief.ui.theme import apply_theme

HOSTILE = '<b>Win</b> & <a href="x">link</a>'


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
