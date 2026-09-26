"""Tolerant substring checks that AI output quotes the email it describes."""

import re
import unicodedata

_STRAIGHT_QUOTES = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
    }
)
_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_STRAIGHT_QUOTES)
    return _WHITESPACE.sub(" ", folded).strip()


def appears_in(fragment: str, *sources: str) -> bool:
    """Whether the normalized fragment is a substring of any normalized source.

    Normalization applies NFKC, case folding, straight quotes and single spaces. An
    empty fragment never matches.
    """
    needle = _normalize(fragment)
    return bool(needle) and any(needle in _normalize(source) for source in sources)
