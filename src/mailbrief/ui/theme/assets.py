"""Bundled Inter (SIL OFL 1.1) and Tabler icons (MIT), resolved beside this module so they
also load from the PyInstaller package."""

import functools
from pathlib import Path
from typing import Final

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

FONT_FAMILY: Final = "Inter"
ICON_NAMES: Final = frozenset(
    {
        "sun",
        "checkbox",
        "hourglass",
        "pencil",
        "calendar",
        "settings",
        "refresh",
        "clock",
        "external-link",
    }
)

_HERE: Final = Path(__file__).resolve().parent
_FONTS: Final = (_HERE / "fonts" / "Inter-Regular.ttf", _HERE / "fonts" / "Inter-Medium.ttf")
_registered = False


def register_fonts() -> None:
    """Load the bundled fonts once per process."""
    global _registered
    if _registered:
        return
    for path in _FONTS:
        if QFontDatabase.addApplicationFont(str(path)) == -1:
            raise RuntimeError("Bundled fonts could not be loaded.")
    _registered = True


def ui_font(px: int, *, medium: bool = False) -> QFont:
    """Inter at a pixel size; point sizes would scale differently on each platform."""
    font = QFont(FONT_FAMILY)
    font.setPixelSize(px)
    font.setWeight(QFont.Weight.Medium if medium else QFont.Weight.Normal)
    return font


@functools.cache
def icon_pixmap(name: str, color: str, px: int, dpr: float = 1.0) -> QPixmap:
    """Render a bundled icon in one colour. Needs a QApplication."""
    if name not in ICON_NAMES:
        raise ValueError("Unknown icon.")
    svg = (_HERE / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    renderer = QSvgRenderer(
        QByteArray(svg.replace('stroke="currentColor"', f'stroke="{color}"').encode())
    )
    size = round(px * dpr)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def icon(name: str, color: str, px: int = 16) -> QIcon:
    result = QIcon()
    for dpr in (1.0, 2.0):
        result.addPixmap(icon_pixmap(name, color, px, dpr))
    return result
