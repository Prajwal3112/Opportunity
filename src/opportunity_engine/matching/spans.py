"""Offset-preserving phrase matching.

Three properties the previous matcher lacked, each of which was load-bearing:

1. **Every occurrence, not the first.** ``re.search`` cannot answer "is *this* occurrence
   inside a blocked phrase", because it only ever sees one.
2. **Character offsets.** Suppression is span containment, so both spans need positions.
3. **A bounded separator.** ``[\\W_]+`` matched across sentence boundaries, table-of-contents
   dot leaders and paragraph breaks, e.g. "road traffic incident. Response times" matched
   the term "incident response".

Offsets index the original text, so evidence snippets remain quotable from the source.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from .normalization import normalize_for_matching, normalize_preserving_case

# Words of a phrase may be separated by spaces, tabs or hyphens, or by a SINGLE line
# wrap. Never by a blank line: after HTML stripping, a paragraph or table-cell boundary
# becomes a double newline, and allowing that would weld two adjacent table cells into
# a phrase the source never said. Sentence punctuation is excluded for the same reason:
# crossing a full stop means the words are in different clauses.
_SEPARATOR = "(?:[ \t­‐-―_’':-]{1,4}|[ \t]*\n[ \t]*)"
_TOKEN = re.compile(r"\w+", re.UNICODE)

# "Security Information & Event Management" is the dominant rendering in tender headings
# and annex titles, and every EXCLUSIVE-tier phrase contains the word "and".
_AND_FORMS = r"(?:and|&|und|et|y|e)"

# Optional plural on any word of three or more characters. Tender text says "Firewalls",
# "Hardware Security Modules", "Intrusion Detection Systems" and "Defects Liability
# Period"; a strict literal matches none of them. Applied to every token, not just the
# last, because the inflected word is often not final.
def _inflect(token: str) -> str:
    escaped = re.escape(token)
    return escaped + "(?:e?s)?" if len(token) >= 3 else escaped


@dataclass(frozen=True)
class Span:
    start: int
    end: int

    def contains(self, other: Span) -> bool:
        return self.start <= other.start and other.end <= self.end


@lru_cache(maxsize=4096)
def phrase_pattern(term: str, *, case_sensitive: bool = False) -> re.Pattern[str] | None:
    """Compile a phrase into a bounded, ampersand-aware pattern.

    Returns None for a term that normalizes to nothing, so a punctuation-only entry
    cannot become a zero-width matcher that fires on every position.
    """
    normalize = normalize_preserving_case if case_sensitive else normalize_for_matching
    tokens = normalize(term).split()
    if not tokens:
        return None
    parts = [_AND_FORMS if token.casefold() == "and" else _inflect(token) for token in tokens]
    pattern = r"(?<!\w)" + _SEPARATOR.join(parts) + r"(?!\w)"
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


def prepare(text: str) -> str:
    """NFKC-normalize without changing length-sensitive structure."""
    return unicodedata.normalize("NFKC", text)


def find_spans(text: str, term: str, *, case_sensitive: bool = False) -> list[Span]:
    """Every occurrence of ``term`` in ``text``, as offsets into ``text``."""
    pattern = phrase_pattern(term, case_sensitive=case_sensitive)
    if pattern is None:
        return []
    return [Span(match.start(), match.end()) for match in pattern.finditer(text)]


@lru_cache(maxsize=256)
def _alternation(terms: tuple[str, ...], *, case_sensitive: bool = False) -> re.Pattern[str] | None:
    """One pattern for a whole phrase set.

    Scanning a field once per phrase dominated runtime: 93 blocked phrases plus 65 terms
    across 6 fields is ~950 separate scans per notice. A single alternation is one pass.
    Longest-first ordering matters because Python alternation is leftmost-*first*, not
    leftmost-longest, so "food security" must be tried before "security".
    """
    normalize = normalize_preserving_case if case_sensitive else normalize_for_matching
    bodies: list[str] = []
    for term in sorted(set(terms), key=len, reverse=True):
        tokens = normalize(term).split()
        if not tokens:
            continue
        parts = [_AND_FORMS if token.casefold() == "and" else _inflect(token) for token in tokens]
        bodies.append(_SEPARATOR.join(parts))
    if not bodies:
        return None
    pattern = r"(?<!\w)(?:" + "|".join(bodies) + r")(?!\w)"
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


def find_any_spans(text: str, terms: list[str], *, case_sensitive: bool = False) -> list[Span]:
    """Every occurrence of any phrase in ``terms``, in a single pass."""
    pattern = _alternation(tuple(terms), case_sensitive=case_sensitive)
    if pattern is None:
        return []
    return [Span(match.start(), match.end()) for match in pattern.finditer(text)]


def find_any_with_text(text: str, terms: list[str], *, case_sensitive: bool = False) -> list[tuple[Span, str]]:
    """As ``find_any_spans``, but also returns the matched surface text."""
    pattern = _alternation(tuple(terms), case_sensitive=case_sensitive)
    if pattern is None:
        return []
    return [(Span(m.start(), m.end()), m.group(0)) for m in pattern.finditer(text)]


def contains_any(text: str, terms: tuple[str, ...], *, case_sensitive: bool = False) -> bool:
    """Cheap prefilter. Under a domain cap ~99% of a corpus matches nothing at all."""
    pattern = _alternation(terms, case_sensitive=case_sensitive)
    return pattern is not None and pattern.search(text) is not None


def token_offsets(text: str) -> list[Span]:
    return [Span(match.start(), match.end()) for match in _TOKEN.finditer(text)]


def window_span(text: str, span: Span, window_tokens: int, tokens: list[Span] | None = None) -> Span:
    """The character range covered by ``window_tokens`` tokens either side of ``span``."""
    tokens = tokens if tokens is not None else token_offsets(text)
    if not tokens:
        return Span(0, len(text))
    inside = [i for i, token in enumerate(tokens) if token.end > span.start and token.start < span.end]
    if not inside:
        return Span(0, len(text))
    low = max(0, inside[0] - window_tokens)
    high = min(len(tokens) - 1, inside[-1] + window_tokens)
    return Span(tokens[low].start, tokens[high].end)


def window_text(text: str, span: Span, window_tokens: int, tokens: list[Span] | None = None) -> str:
    """The text spanned by ``window_tokens`` tokens either side of ``span``.

    A window is a clause-sized neighbourhood. Evaluated over a whole document a context
    requirement becomes a no-op, because a long document mentions almost everything
    somewhere; the window is what keeps a gate meaningful.
    """
    tokens = tokens if tokens is not None else token_offsets(text)
    if not tokens:
        return text
    inside = [index for index, token in enumerate(tokens) if token.end > span.start and token.start < span.end]
    if not inside:
        return text
    low = max(0, inside[0] - window_tokens)
    high = min(len(tokens) - 1, inside[-1] + window_tokens)
    return text[tokens[low].start : tokens[high].end]


def snippet(text: str, span: Span, radius: int = 90) -> str:
    start = max(0, span.start - radius)
    end = min(len(text), span.end + radius)
    return text[start:end].strip()


def is_suppressed(span: Span, blocked: list[Span]) -> bool:
    """True when this occurrence sits wholly inside a blocked phrase.

    Containment rather than co-occurrence is the point. A digital-government tender may
    legitimately mention 'food security' in its background and 'SIEM' in its scope;
    document-level negation would discard the genuine opportunity.
    """
    return any(block.contains(span) for block in blocked)
