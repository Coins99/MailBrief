"""Design tokens: the only place MailBrief's UI colours are defined.

DARK is measured from the approved mockup (docs/ui/mockup-three-pane-dark.png); LIGHT is
provisional until a light mockup is approved.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ThemeMode(StrEnum):
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True, slots=True)
class Tokens:
    canvas: str  # Header bar.
    panel: str  # Window, panes and cards.
    selection: str  # Selected row and navigation item, hover.
    hairline: str  # Dividers and card borders.
    border_strong: str  # Outline buttons.
    accent_border: str  # Selected-row bar.
    text: str  # Titles and body.
    text_secondary: str  # Senders and meta.
    text_muted: str  # Section labels and notes.
    warning_fg: str  # Deadlines.
    warning_bg: str
    accent_fg: str  # Proposal chips and focus.
    accent_bg: str


DARK: Final = Tokens(
    canvas="#151515",
    panel="#1a1a19",
    selection="#151515",
    hairline="#313130",
    border_strong="#484847",
    accent_border="#193567",
    text="#f0efec",
    text_secondary="#c3c2b8",
    text_muted="#898782",
    warning_fg="#d19633",
    warning_bg="#2e1b04",
    accent_fg="#7aa5e6",
    accent_bg="#0b1f40",
)

LIGHT: Final = Tokens(
    canvas="#f5f4f0",
    panel="#ffffff",
    selection="#f5f4f0",
    hairline="#e3e2dd",
    border_strong="#c9c8c2",
    accent_border="#9dbbe8",
    text="#1f1e1b",
    text_secondary="#5f5e5a",
    text_muted="#6f6e69",
    warning_fg="#8a5a00",
    warning_bg="#fbefd9",
    accent_fg="#1d4f91",
    accent_bg="#e3edfb",
)

# Metrics, in logical pixels.
RADIUS: Final = 8
SIDEBAR_WIDTH: Final = 128
TEXT_PX: Final = 13
SMALL_PX: Final = 12
CAPTION_PX: Final = 11
TITLE_PX: Final = 15


def tokens_for(mode: ThemeMode) -> Tokens:
    return DARK if mode is ThemeMode.DARK else LIGHT
