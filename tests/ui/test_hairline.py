"""Cosmetic hairlines: cards, dividers and the splitter handle paint with token colours."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from mailbrief.ui.hairline import HairlineDivider, HairlineFrame, HairlineSplitter, hairline_pen
from mailbrief.ui.theme import DARK, apply_theme


def test_hairline_pen_is_cosmetic() -> None:
    pen = hairline_pen(DARK.hairline)
    assert pen.isCosmetic() and pen.width() == 0
    assert pen.color() == QColor(DARK.hairline)


def test_card_divider_and_splitter_paint_hairlines(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp)
    card = HairlineFrame(strong=True)
    qtbot.addWidget(card)
    card.resize(120, 60)
    assert card.contentsMargins().left() == 12 and card.contentsMargins().top() == 10
    image = card.grab().toImage()
    middle = image.height() // 2
    assert image.pixelColor(0, middle) == QColor(DARK.border_strong)

    divider = HairlineDivider(Qt.Orientation.Vertical)
    qtbot.addWidget(divider)
    divider.resize(1, 40)
    assert divider.width() == 1
    assert divider.grab().toImage().pixelColor(0, 20) == QColor(DARK.hairline)
    horizontal = HairlineDivider()
    qtbot.addWidget(horizontal)
    assert horizontal.maximumHeight() == 1
    assert not horizontal.grab().isNull()

    splitter = HairlineSplitter()
    qtbot.addWidget(splitter)
    splitter.addWidget(QLabel("left"))
    splitter.addWidget(QLabel("right"))
    splitter.resize(200, 50)
    splitter.show()
    assert splitter.handleWidth() == 1
    assert not splitter.handle(1).grab().isNull()
