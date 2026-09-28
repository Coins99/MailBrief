"""Tolerant substring checks that AI output quotes, or copies, the email it describes."""

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


def copies_long_run(generated: str, source: str, limit: int) -> bool:
    """Whether any ``limit``-character run of ``generated`` appears in ``source``.

    Both are normalized as appears_in does, so changing case, quotes or spacing does not
    hide a copy. Every window of the source goes into a set, so each window of the generated
    text is one lookup instead of a scan of the source. A limit below 1 is refused.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    copied = _normalize(generated)
    original = _normalize(source)
    if len(copied) < limit or len(original) < limit:
        return False
    windows = {original[start : start + limit] for start in range(len(original) - limit + 1)}
    return any(copied[start : start + limit] in windows for start in range(len(copied) - limit + 1))
