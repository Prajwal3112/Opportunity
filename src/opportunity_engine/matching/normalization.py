"""Safe matching normalization. It never changes stored source text."""
from __future__ import annotations

import re
import unicodedata

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_for_matching(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = _PUNCTUATION.sub(" ", value)
    return _WHITESPACE.sub(" ", value).strip()


def normalize_preserving_case(value: str) -> str:
    """As ``normalize_for_matching`` but without case folding.

    Required for case-sensitive terms: folding the term to lowercase and then compiling
    without IGNORECASE produces a pattern that matches only the lowercase form, which is
    the opposite of what a case-sensitive acronym needs.
    """
    value = unicodedata.normalize("NFKC", value)
    value = _PUNCTUATION.sub(" ", value)
    return _WHITESPACE.sub(" ", value).strip()


def unique_phrases(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_for_matching(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result
