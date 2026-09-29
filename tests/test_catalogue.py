"""The product catalogue is edited by a non-technical user at runtime.

Every test here is about the same property: a bad edit must be rejected *before* the
file changes, because a broken catalogue takes the whole tool down and the person who
broke it has no way to repair a 2,000-line JSON file.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from wb_connector import catalogue

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "cybersecurity.json"


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    target = tmp_path / "cyber.json"
    shutil.copy(PROFILE, target)
    return target


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_add_appends_a_term_and_bumps_the_version(profile: Path) -> None:
    before = read(profile)
    version = catalogue.add_product(profile, "Zero Trust Network Access")
    after = read(profile)

    assert len(after["terms"]) == len(before["terms"]) + 1
    assert version != before["taxonomy_version"]
    assert after["taxonomy_version"] == version
    added = next(t for t in after["terms"] if t["id"] == "product_zero_trust_network_access")
    # CORROBORATING, not EXCLUSIVE: "this term can stand alone as evidence" is a claim
    # about the whole corpus, and nobody can make it while typing into a text box.
    assert added["tier"] == "CORROBORATING"


def test_whitespace_is_collapsed_so_two_spellings_cannot_both_be_added(profile: Path) -> None:
    catalogue.add_product(profile, "Threat   Intelligence")
    with pytest.raises(catalogue.CatalogueError, match="already in the catalogue"):
        catalogue.add_product(profile, " Threat Intelligence ")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("data(loss)prevention", "alternation"),   # Would silently match nothing.
        ("", "Type the product name"),
        ("   ", "Type the product name"),
        ("!!!", "no letters or digits"),
        ("x" * 121, "too long"),
    ],
)
def test_a_rejected_add_leaves_the_file_byte_identical(profile: Path, text: str, message: str) -> None:
    original = profile.read_bytes()
    with pytest.raises(catalogue.CatalogueError, match=message):
        catalogue.add_product(profile, text)
    assert profile.read_bytes() == original


def test_remove_restores_the_original_term_set(profile: Path) -> None:
    before = read(profile)["terms"]
    catalogue.add_product(profile, "Zero Trust Network Access")
    catalogue.remove_product(profile, "product_zero_trust_network_access")
    assert read(profile)["terms"] == before


def test_a_hand_written_term_cannot_be_removed_from_the_screen(profile: Path) -> None:
    """Hand-written terms carry context guards and blocklists a text box cannot express,
    so removing one through the UI would discard reasoning nobody can see."""
    hand_written = read(profile)["terms"][0]["id"]
    assert not hand_written.startswith("product_")
    with pytest.raises(catalogue.CatalogueError, match="written by hand"):
        catalogue.remove_product(profile, hand_written)


def test_removing_an_absent_term_is_refused(profile: Path) -> None:
    original = profile.read_bytes()
    with pytest.raises(catalogue.CatalogueError, match="no longer in the catalogue"):
        catalogue.remove_product(profile, "product_never_existed")
    assert profile.read_bytes() == original


def test_the_saved_catalogue_still_loads_and_lints(profile: Path) -> None:
    from opportunity_engine.domain.taxonomy import load_taxonomy

    catalogue.add_product(profile, "Zero Trust Network Access")
    taxonomy = load_taxonomy(profile, strict=True)
    assert any(term.text == "Zero Trust Network Access" for term in taxonomy.terms)


def test_version_bump_preserves_a_suffix() -> None:
    assert catalogue._bump("2026.09.3-pilot") == "2026.09.4-pilot"
    assert catalogue._bump("1.0.9") == "1.0.10"
    assert catalogue._bump("nodigits") == "nodigits.1"


def test_remove_reports_the_text_so_the_caller_can_stop_searching(profile: Path) -> None:
    """The dashboard removes the matching search term straight after, and would
    otherwise have to re-read the file to learn what phrase to drop."""
    catalogue.add_product(profile, "Zero Trust Network Access")
    assert catalogue.remove_product(profile, "product_zero_trust_network_access") == (
        "Zero Trust Network Access"
    )
