"""Bundled Inter (SIL OFL 1.1) and Tabler icons (MIT), resolved beside this module so they
also load from the PyInstaller package."""

import functools
from pathlib import Path
from typing import Final

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QIcon, QPainter, QPixmap
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
        "mail",
        "database",
    }
)

_HERE: Final = Path(__file__).resolve().parent
_FONTS: Final = (_HERE / "fonts" / "Inter-Regular.ttf", _HERE / "fonts" / "Inter-Medium.ttf")
_fonts_loaded: bool | None = None  # None until the first attempt; then cached.


def register_fonts() -> bool:
    """Load the bundled fonts once per process; True only when both loaded.

    A missing or unreadable font never stops the app: the theme then keeps Qt's font.
    """
    global _fonts_loaded
    if _fonts_loaded is None:
        _fonts_loaded = all(QFontDatabase.addApplicationFont(str(path)) != -1 for path in _FONTS)
        release_font_cache()
    return _fonts_loaded


@functools.cache
def _font(px: int, medium: bool) -> QFont:
    font = QFont(FONT_FAMILY)
    font.setPixelSize(px)
    font.setWeight(QFont.Weight.Medium if medium else QFont.Weight.Normal)
    return font


def ui_font(px: int, *, medium: bool = False) -> QFont:
    """Inter at a pixel size; point sizes would scale differently on each platform. A copy,
    so a caller that changes it never changes the cached font."""
    return QFont(_font(px, medium))


@functools.cache
def _metrics(px: int, medium: bool) -> QFontMetrics:
    return QFontMetrics(_font(px, medium))


def ui_metrics(px: int, *, medium: bool = False) -> QFontMetrics:
    """The metrics of ``ui_font(px, medium=medium)``, built once per size and weight."""
    return _metrics(px, medium)


def release_font_cache() -> None:
    """Drop the cached fonts and metrics: when the bundled fonts are registered, so nothing
    measured with a fallback font is reused, and when the app is about to quit."""
    _font.cache_clear()
    _metrics.cache_clear()


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


def release_icon_cache() -> None:
    """Drop the cached icon pixmaps; ``apply_theme`` runs this when the app is about to quit,
    while Qt can still free them."""
    icon_pixmap.cache_clear()


def icon(name: str, color: str, px: int = 16) -> QIcon:
    result = QIcon()
    for dpr in (1.0, 2.0):
        result.addPixmap(icon_pixmap(name, color, px, dpr))
    return result
