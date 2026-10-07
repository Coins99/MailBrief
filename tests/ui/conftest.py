"""Qt UI tests need a WindowServer session on macOS."""

import os
import sys
from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication, QProxyStyle

from mailbrief.ui.theme import DARK, set_current_tokens


@pytest.fixture(autouse=True)
def require_gui_session() -> None:
    if (
        sys.platform == "darwin"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("MACOS_GUI_AVAILABLE")
    ):
        pytest.skip("Requires active macOS GUI WindowServer session")


@pytest.fixture
def themed(qapp: QApplication) -> Iterator[None]:
    """Restore the application's look after a test that applies the theme, so no other
    test ever sees Fusion, the palette, the stylesheet or the bundled font."""
    stylesheet = qapp.styleSheet()
    qapp.setStyleSheet("")  # A stylesheet's proxy style hides the real style's name.
    current = qapp.style()
    # The theme's own proxy style has no name; its base style does.
    style = (current.baseStyle() if isinstance(current, QProxyStyle) else current).name()
    palette = qapp.palette()
    font = qapp.font()
    qapp.setStyleSheet(stylesheet)
    try:
        yield
    finally:
        qapp.setStyleSheet("")
        qapp.setStyle(style)
        qapp.setPalette(palette)
        qapp.setFont(font)
        qapp.setStyleSheet(stylesheet)
        set_current_tokens(DARK)
