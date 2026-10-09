"""Cosmetic hairlines: cards, dividers and the splitter handle paint with token colours."""

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QStyle
from pytestqt.qtbot import QtBot

from mailbrief.ui.hairline import (
    HairlineDivider,
    HairlineFrame,
    HairlineSplitter,
    hairline_pen,
    keyboard_focus,
    paint_focus_ring,
)
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
    assert splitter.handleWidth() == 5  # Wide enough to grab.
    handle = splitter.handle(1).grab().toImage()
    # Grabs are in device pixels: 5 at 1x, 10 on a 2x screen.
    pixels = round(5 * handle.devicePixelRatio())
    assert handle.width() == pixels
    row = [handle.pixelColor(x, 10) for x in range(pixels)]
    # One device-pixel line at the centre, panel colour either side.
    assert row.count(QColor(DARK.hairline)) == 1
    assert row[(pixels - 1) // 2] == QColor(DARK.hairline)
    assert row[0] == row[-1] == QColor(DARK.panel)


FOCUS = QStyle.StateFlag.State_HasFocus
KEYBOARD = QStyle.StateFlag.State_KeyboardFocusChange


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (QStyle.StateFlag.State_None, False),
        (FOCUS, False),
        (KEYBOARD, False),
        (FOCUS | KEYBOARD, True),
    ],
)
def test_the_focus_ring_needs_keyboard_focus(state: QStyle.StateFlag, expected: bool) -> None:
    assert keyboard_focus(state) is expected


def ring_image(state: QStyle.StateFlag) -> QImage:
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_focus_ring(painter, QRect(0, 0, 20, 20), state, DARK.accent_fg)
    painter.end()
    return image


def transparent(image: QImage) -> bool:
    return all(
        image.pixelColor(x, y).alpha() == 0
        for x in range(image.width())
        for y in range(image.height())
    )


def test_the_focus_ring_paints_only_after_keyboard_movement(qapp: QApplication) -> None:
    assert transparent(ring_image(FOCUS))
    assert not transparent(ring_image(FOCUS | KEYBOARD))
