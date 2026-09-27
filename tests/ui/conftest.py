"""Qt UI tests need a WindowServer session on macOS."""

import os
import sys

import pytest


@pytest.fixture(autouse=True)
def require_gui_session() -> None:
    if (
        sys.platform == "darwin"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("MACOS_GUI_AVAILABLE")
    ):
        pytest.skip("Requires active macOS GUI WindowServer session")
