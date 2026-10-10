"""Rules for the source itself, so a slip shows up in the tests rather than on screen. Each
failure lists the offending lines as "path:line: text"."""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
SOURCE: Final = ROOT / "src" / "mailbrief"
UI: Final = SOURCE / "ui"
TOKENS: Final = UI / "theme" / "tokens.py"

# strftime directives that print words or formats of the system language: weekday and month
# names (%a %A %b %B), AM or PM (%p), and the system's date and date-time formats (%c %x).
# Qt sets the C library's locale from the system language, so they would follow it. "%%" is
# a literal percent sign, and a directive followed by a letter or digit is something else,
# such as "%B3" in a percent-encoded URL or "%abc%" in a LIKE pattern.
SYSTEM_LANGUAGE_DATE: Final = re.compile(r"(?<!%)%[aAbBpcx](?![0-9A-Za-z])")
# A colour written as six or eight hex digits after "#".
HEX_COLOUR: Final = re.compile(r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6})(?![0-9A-Za-z])")


def offending_lines(files: Iterable[Path], pattern: re.Pattern[str]) -> list[str]:
    """Every line of ``files`` that ``pattern`` matches, as "path:line: text"."""
    found: list[str] = []
    for path in sorted(files):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if pattern.search(line):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()}")
    return found


def test_no_date_format_follows_the_system_language() -> None:
    """Weekday and month names come from the English tables in ``ui/deadline_text.py``."""
    assert offending_lines(SOURCE.rglob("*.py"), SYSTEM_LANGUAGE_DATE) == []


def test_ui_colours_come_only_from_the_tokens() -> None:
    """Every colour under ``ui`` is a design token from ``ui/theme/tokens.py``."""
    files = (path for path in UI.rglob("*.py") if path != TOKENS)
    assert offending_lines(files, HEX_COLOUR) == []


def test_the_rules_find_what_they_are_for() -> None:
    """The patterns catch the slips they exist for, and pass what only looks like them."""
    for line in ('f"{day:%b} {day.day}"', 'when.strftime("%a %d %B")', '"%I:%M %p"', '"%x"'):
        assert SYSTEM_LANGUAGE_DATE.search(line), line
    for line in ('"%Y-%m-%d %H:%M"', '"100%%a"', '"%B3%A9"', "LIKE '%abc%'", '"%s" % name'):
        assert not SYSTEM_LANGUAGE_DATE.search(line), line
    for line in ('QColor("#1a1a19")', "color: #193567;", '"#FF193567"'):
        assert HEX_COLOUR.search(line), line
    for line in ("QListView#briefList", "# PR #38 and #2041", '"#1a1a1"', "#accent_fg"):
        assert not HEX_COLOUR.search(line), line
