"""Design tokens, palette, stylesheet and bundled assets."""

import dataclasses
import re

import pytest
from PySide6.QtGui import QFontInfo, QPalette
from PySide6.QtWidgets import QApplication

from mailbrief.ui.theme import (
    DARK,
    ThemeMode,
    Tokens,
    apply_theme,
    build_stylesheet,
    current_tokens,
    icon_pixmap,
    register_fonts,
    tokens_for,
    ui_font,
)


def luminance(color: str) -> float:
    """WCAG 2.x relative luminance of a #rrggbb colour."""
    channels = []
    for index in (1, 3, 5):
        value = int(color[index : index + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_formula_matches_wcag_reference_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_every_token_is_a_lowercase_hex_colour(mode: ThemeMode) -> None:
    for value in dataclasses.asdict(tokens_for(mode)).values():
        assert re.fullmatch(r"#[0-9a-f]{6}", value)


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_text_meets_wcag_aa_contrast(mode: ThemeMode) -> None:
    t = tokens_for(mode)
    pairs = [
        (foreground, background)
        for foreground in (t.text, t.text_secondary, t.text_muted)
        for background in (t.panel, t.selection)
    ]
    pairs += [(t.warning_fg, t.warning_bg), (t.accent_fg, t.accent_bg)]
    for foreground, background in pairs:
        assert contrast(foreground, background) >= 4.5, (mode, foreground, background)


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_stylesheet_substitutes_every_token(mode: ThemeMode) -> None:
    stylesheet = build_stylesheet(tokens_for(mode))
    assert "$" not in stylesheet
    assert tokens_for(mode).panel in stylesheet


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_apply_theme_sets_style_palette_and_tokens(
    qapp: QApplication, themed: None, mode: ThemeMode
) -> None:
    tokens = apply_theme(qapp, mode)
    assert isinstance(tokens, Tokens)
    assert qapp.styleSheet() == build_stylesheet(tokens)
    palette = qapp.palette()
    assert palette.color(QPalette.ColorRole.Window).name() == tokens.panel
    assert palette.color(QPalette.ColorRole.Accent).name() == tokens.accent_fg
    assert palette.color(QPalette.ColorRole.Midlight).name() == tokens.selection
    assert palette.color(QPalette.ColorRole.ToolTipBase).name() == tokens.canvas
    assert current_tokens() == tokens_for(mode)
    qapp.setStyleSheet("")  # The stylesheet's proxy hides the base style's name.
    assert qapp.style().name() == "fusion"


def test_themed_fixture_restores_dark_tokens() -> None:
    assert current_tokens() == DARK


def test_medium_font_resolves_to_bundled_weight(qapp: QApplication, themed: None) -> None:
    apply_theme(qapp)
    font = ui_font(13, medium=True)
    assert QFontInfo(font).weight() == 500
    assert QFontInfo(font).family() == "Inter"
    assert font.pixelSize() == 13


def test_icon_pixmap_renders_at_device_pixel_ratio(qapp: QApplication) -> None:
    pixmap = icon_pixmap("refresh", DARK.text, 16, 2.0)
    assert not pixmap.isNull()
    assert pixmap.devicePixelRatio() == 2.0
    assert pixmap.width() == 32
    assert pixmap.toImage().pixelColor(0, 0).alpha() == 0


def test_unknown_icon_is_refused(qapp: QApplication) -> None:
    with pytest.raises(ValueError):
        icon_pixmap("../../secrets", DARK.text, 16)


def test_registering_fonts_twice_is_harmless(qapp: QApplication) -> None:
    register_fonts()
    register_fonts()
