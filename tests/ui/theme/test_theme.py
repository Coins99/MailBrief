"""Design tokens, palette, stylesheet and bundled assets."""

import dataclasses
import re

import pytest
from PySide6.QtCore import QMetaMethod
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics, QPalette
from PySide6.QtWidgets import QApplication, QStyle

from mailbrief.ui.theme import (
    DARK,
    ThemeMode,
    Tokens,
    apply_theme,
    assets,
    build_stylesheet,
    current_tokens,
    icon_pixmap,
    register_fonts,
    release_font_cache,
    tokens_for,
    ui_font,
    ui_metrics,
)
from mailbrief.ui.theme.style import ThemeStyle


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
    style = qapp.style()
    assert isinstance(style, ThemeStyle) and style.baseStyle().name() == "fusion"


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


def test_missing_fonts_still_apply_the_colours(
    qapp: QApplication, themed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assets, "_fonts_loaded", None)
    monkeypatch.setattr(QFontDatabase, "addApplicationFont", lambda path: -1)
    qapp.setFont(QFont("Helvetica", 9))
    before = qapp.font().family()
    tokens = apply_theme(qapp, ThemeMode.DARK)
    assert register_fonts() is False  # Cached: a failed load isn't retried.
    assert tokens == DARK and current_tokens() == DARK
    assert qapp.font().family() == before
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == DARK.panel
    assert qapp.styleSheet() == build_stylesheet(DARK)


def test_releasing_the_icon_cache_empties_it(qapp: QApplication, themed: None) -> None:
    apply_theme(qapp)
    icon_pixmap("sun", DARK.text, 16)
    assert icon_pixmap.cache_info().currsize > 0
    assets.release_icon_cache()
    assert icon_pixmap.cache_info().currsize == 0
    # apply_theme runs it on aboutToQuit; tests never emit the shared app's signals.
    assert qapp.isSignalConnected(QMetaMethod.fromSignal(qapp.aboutToQuit))


def test_disabled_outline_buttons_are_muted() -> None:
    sheet = build_stylesheet(DARK)
    assert (
        'QPushButton[variant="outline"]:disabled '
        f"{{ color: {DARK.text_muted}; border-color: {DARK.hairline}; }}"
    ) in sheet


def test_the_theme_style_never_underlines_mnemonics(qapp: QApplication, themed: None) -> None:
    apply_theme(qapp)
    hint = QStyle.StyleHint.SH_UnderlineShortcut
    assert qapp.style().styleHint(hint) == 0
    qapp.setStyleSheet("")
    style = qapp.style()
    assert isinstance(style, ThemeStyle)
    # Every other hint is Fusion's own.
    fusion = style.baseStyle()
    other = QStyle.StyleHint.SH_DialogButtonBox_ButtonsHaveIcons
    assert style.styleHint(other) == fusion.styleHint(other)
    apply_theme(qapp)  # Applying again keeps the same style.
    qapp.setStyleSheet("")
    assert qapp.style() is style


def test_a_changed_font_never_changes_the_cache(qapp: QApplication) -> None:
    font = ui_font(13, medium=True)
    font.setPixelSize(40)
    assert ui_font(13, medium=True).pixelSize() == 13


def test_metrics_are_cached_per_size_and_weight(qapp: QApplication) -> None:
    assert ui_metrics(13) is ui_metrics(13)
    assert ui_metrics(13) is not ui_metrics(13, medium=True)
    assert ui_metrics(12).height() == QFontMetrics(ui_font(12)).height()


def test_releasing_the_font_cache_measures_again(qapp: QApplication) -> None:
    cached = ui_metrics(11)
    release_font_cache()
    assert ui_metrics(11) is not cached


def test_registering_the_fonts_drops_cached_metrics(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = ui_metrics(11)
    monkeypatch.setattr(assets, "_fonts_loaded", None)
    register_fonts()
    assert ui_metrics(11) is not cached
