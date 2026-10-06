"""Plain-text labels, escaped button text and the elided coverage line."""

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from mailbrief.ui.labels import ElidedLabel, button_label, plain_label, short_button_label

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
