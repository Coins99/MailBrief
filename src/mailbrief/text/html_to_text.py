"""Convert untrusted email HTML to plain text using only the standard library."""

import re
from html.parser import HTMLParser

_SKIPPED = frozenset(
    {"head", "math", "noscript", "object", "script", "style", "svg", "template", "title"}
)
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_BLOCK = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)
_LINE_ITEMS = frozenset({"dd", "dt", "li", "tr"})
_HIDDEN_STYLE = re.compile(
    r"display:none|visibility:hidden|mso-hide:all"
    r"|opacity:0(?![.\d])|font-size:0(?![.\d])|max-height:0(?![.\d])"
)
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))
_DECIMAL_REF = re.compile(r"&#(\d+)(;?)")


def _shorten_decimal_ref(match: re.Match[str]) -> str:
    digits = match.group(1).lstrip("0") or "0"
    return "\ufffd" if len(digits) > 7 else f"&#{digits}{match.group(2)}"


def bound_numeric_refs(text: str) -> str:
    """Shorten decimal character references so that unescaping them cannot fail.

    Python refuses to convert more than 4,300 digits, so a crafted reference would make
    ``html.unescape`` raise. Values that long are never valid characters and become U+FFFD,
    as ``html.unescape`` does for any out-of-range reference.
    """
    return _DECIMAL_REF.sub(_shorten_decimal_ref, text)


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "style" and value and _HIDDEN_STYLE.search(value.lower().replace(" ", "")):
            return True
    return False


class _TextBuilder(HTMLParser):
    """Collect visible text; quoted blocks become '> ' lines like plain-text replies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._open: list[tuple[str, bool]] = []
        self._open_counts: dict[str, int] = {}
        self._lines: list[tuple[int, list[str]]] = [(0, [])]
        self._quote_depth = 0

    def _inside_hidden(self) -> bool:
        return bool(self._open) and self._open[-1][1]

    def _new_line(self) -> None:
        self._lines.append((self._quote_depth, []))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = self._inside_hidden() or tag in _SKIPPED or _is_hidden(attrs)
        if tag in _VOID:
            if tag in {"br", "hr"} and not hidden:
                self._new_line()
            return
        self._open.append((tag, hidden))
        self._open_counts[tag] = self._open_counts.get(tag, 0) + 1
        if hidden:
            return
        if tag == "blockquote":
            self._quote_depth += 1
        if tag in _BLOCK:
            self._new_line()
        if tag == "li":
            self._lines[-1][1].append("- ")
        elif tag in {"td", "th"}:
            self._lines[-1][1].append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        # Unmatched end tags return at once, so crafted markup can't force a stack scan each.
        if tag in _VOID or not self._open_counts.get(tag):
            return
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] != tag:
                continue
            closed = self._open[index:]
            del self._open[index:]
            for name, hidden in closed:
                self._open_counts[name] -= 1
                if name == "blockquote" and not hidden:
                    self._quote_depth -= 1
            if tag in _BLOCK and tag not in _LINE_ITEMS and not closed[0][1]:
                self._new_line()
            return

    def handle_data(self, data: str) -> None:
        if self._inside_hidden():
            return
        depth, fragments = self._lines[-1]
        if not fragments and depth != self._quote_depth:
            self._lines[-1] = (self._quote_depth, fragments)
        fragments.append(data)

    def text(self) -> str:
        rendered: list[str] = []
        for depth, fragments in self._lines:
            line = " ".join("".join(fragments).translate(_INVISIBLE).split())
            rendered.append("> " * depth + line if line else "")
        return re.sub(r"\n{3,}", "\n\n", "\n".join(rendered)).strip()


def html_to_text(markup: str) -> str:
    """Readable text from untrusted HTML, without scripts, styles, hidden text, images or URLs.

    Nothing is fetched. Text inside ``<blockquote>`` is prefixed with ``> `` so quoted
    history can be recognized the same way as in plain-text replies.
    """
    builder = _TextBuilder()
    builder.feed(bound_numeric_refs(markup))
    builder.close()
    return builder.text()
