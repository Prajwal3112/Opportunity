"""Turn the World Bank's HTML notice bodies into plain text.

Measured: 99.9% of populated ``notice_text`` values contain markup. The phrase matcher
joins words with ``[\\s­‐-―_-]{1,4}`` (`spans.py`), which admits whitespace and hyphens
but not tags -- so ``<b>security information</b> and event management`` silently fails
to match the term ``security information and event management``. Every multi-word term
is exposed to this, which makes stripping a recall fix, not a cosmetic one.

Standard library only; the markup in this corpus is Word-generated HTML fragments, not
documents needing a real parser.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# Tags whose *content* is not prose and must be dropped, not just unwrapped.
_DROP_CONTENT = frozenset({"script", "style", "head", "title"})

# Tags that imply a text break. Without these, "</p><p>" would weld two words together
# and invent phrases that were never adjacent -- the opposite failure to the one being
# fixed, and a worse one.
_BREAKING = frozenset({
    "p", "div", "br", "tr", "td", "th", "li", "ul", "ol", "table", "thead", "tbody",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "hr",
})

_WHITESPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _DROP_CONTENT:
            self._suppress += 1
        elif tag in _BREAKING:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT:
            self._suppress = max(0, self._suppress - 1)
        elif tag in _BREAKING:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self.parts.append(data)


def strip_html(value: str | None) -> str:
    """Plain text from a notice body, safe to match against and to show a human.

    Non-breaking spaces become ordinary spaces: the matcher's separator class does not
    include U+00A0, so ``data&nbsp;loss prevention`` would otherwise never match.
    """
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return _tidy(value)
    parser = _Stripper()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 - any parser failure must degrade, never propagate
        # Notice bodies are untrusted third-party markup. A malformed fragment must fall
        # back to a usable string rather than abort a whole result set.
        text = unescape(re.sub(r"<[^>]*>", " ", value))
    return _tidy(text)


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def first_sentence(text: str, limit: int = 180) -> str:
    """A one-line summary for a notice whose title is blank."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    match = re.search(r"(?<=[.!?])\s", cleaned[: limit + 60])
    sentence = cleaned[: match.start()] if match else cleaned[:limit]
    return sentence if len(sentence) <= limit else sentence[:limit].rsplit(" ", 1)[0] + "…"
