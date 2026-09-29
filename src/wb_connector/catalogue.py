"""Editing the product catalogue at runtime.

The taxonomy JSON *is* the product catalogue: each term is a thing the organisation
sells or a phrase its buyers use. Products evolve -- a module is added, a name changes,
a word turns out to match nothing -- and until now that meant hand-editing a 2,000-line
JSON file, which is not a thing a business developer will do, so in practice the
catalogue would have frozen on the day it was written.

Three properties make runtime editing safe rather than reckless:

1. **Validated before it is saved.** A write that fails Pydantic validation or the
   taxonomy linter is rejected with the reason, and the file on disk is untouched. The
   linter is what catches a phrase that cannot match its own text -- a silent
   zero-recall rule, the single easiest way to break this product.
2. **Written atomically.** A temp file in the same directory, then ``os.replace``. A
   crash mid-write cannot leave a truncated profile, which would take the tool down.
3. **Versioned.** Every save bumps the patch component of ``taxonomy_version``. That
   version is part of the label candidate key, so old labels stay attributable to the
   catalogue that produced them instead of being silently reinterpreted.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from opportunity_engine.domain.taxonomy import DomainTaxonomy, TermTier

# A new product term is CORROBORATING by default, not EXCLUSIVE. An EXCLUSIVE term can
# stand alone as evidence, which is a claim about the whole corpus that nobody can make
# while typing into a text box. Promotion is a deliberate edit to the file.
DEFAULT_TIER = TermTier.CORROBORATING
DEFAULT_CATEGORY = "product"

_SLUG = re.compile(r"[^a-z0-9]+")


class CatalogueError(ValueError):
    """A rejected edit. The message is written to be shown to a non-technical user."""


@dataclass(frozen=True)
class CatalogueEntry:
    id: str
    text: str
    tier: str
    category: str
    editable: bool


def term_id(text: str) -> str:
    slug = _SLUG.sub("_", text.strip().casefold()).strip("_")
    if not slug:
        raise CatalogueError("That name has no letters or digits in it.")
    return f"product_{slug}"


def entries(taxonomy: DomainTaxonomy) -> list[CatalogueEntry]:
    """The catalogue as a user sees it, own-products first.

    ``editable`` marks the terms this module created. A term written by hand may carry a
    context guard, a blocklist or a case rule that a text box cannot express, so deleting
    it through the UI would quietly discard reasoning nobody can see. Those are shown but
    not removable.
    """
    out = [
        CatalogueEntry(
            id=t.id, text=t.text, tier=str(t.tier), category=t.category,
            editable=t.id.startswith("product_") and t.requires_context is None
            and t.blocked_context is None and not t.blocked_ngrams,
        )
        for t in taxonomy.terms
    ]
    out.sort(key=lambda e: (not e.editable, e.text.casefold()))
    return out


def _bump(version: str) -> str:
    """Bump the trailing integer of a version, preserving any suffix like '-pilot'."""
    match = re.match(r"^(.*?)(\d+)([^\d]*)$", version)
    if not match:
        return f"{version}.1"
    head, number, tail = match.groups()
    return f"{head}{int(number) + 1}{tail}"


def _validated(payload: dict) -> DomainTaxonomy:
    """Validate and lint, translating both into messages a user can act on."""
    try:
        taxonomy = DomainTaxonomy.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError, or a bad shape
        raise CatalogueError(f"The catalogue would no longer be valid: {exc}") from exc
    problems = taxonomy.lint()
    if problems:
        raise CatalogueError(problems[0])
    return taxonomy


def _write(path: Path, payload: dict) -> None:
    """Atomic replace, so a crash cannot leave a half-written catalogue."""
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def add_product(path: Path, text: str) -> str:
    """Add a product term. Returns the new taxonomy version."""
    text = " ".join(text.split())
    if not text:
        raise CatalogueError("Type the product name first.")
    if len(text) > 120:
        raise CatalogueError("That is too long for a product name.")

    payload = json.loads(path.read_text(encoding="utf-8"))
    new_id = term_id(text)
    if any(t["id"] == new_id for t in payload["terms"]):
        raise CatalogueError(f"{text!r} is already in the catalogue.")

    payload["terms"].append(
        {"id": new_id, "text": text, "tier": str(DEFAULT_TIER), "category": DEFAULT_CATEGORY,
         "notes": "Added from the dashboard."}
    )
    payload["taxonomy_version"] = _bump(payload["taxonomy_version"])
    _validated(payload)  # Raises before anything is written.
    _write(path, payload)
    return payload["taxonomy_version"]


def remove_product(path: Path, term_identifier: str) -> str:
    """Remove a dashboard-added product term. Returns the removed term's text.

    The text, not the version, because the caller's next job is to stop searching for
    that phrase and it would otherwise have to re-read the file to learn what it was.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed = next((t["text"] for t in payload["terms"] if t["id"] == term_identifier), "")
    remaining = [t for t in payload["terms"] if t["id"] != term_identifier]
    if len(remaining) == len(payload["terms"]):
        raise CatalogueError("That product is no longer in the catalogue.")
    if not term_identifier.startswith("product_"):
        raise CatalogueError(
            "That term was written by hand and may carry matching rules this screen "
            "cannot show. Edit the profile file to remove it."
        )
    if not remaining:
        raise CatalogueError("The catalogue cannot be empty -- nothing would ever match.")

    payload["terms"] = remaining
    payload["taxonomy_version"] = _bump(payload["taxonomy_version"])
    _validated(payload)
    _write(path, payload)
    return removed
