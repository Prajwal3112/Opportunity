"""Filtering by country and region.

A business developer sells into some markets and not others, and the World Bank files a
third of these notices under regional names rather than countries, so the matching rule
has to cope with both spellings of the same place.
"""
from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DASHBOARD_DB", str(tmp_path / "t.db"))
    from wb_connector import dashboard

    importlib.reload(dashboard)
    return dashboard


def notice(place: str) -> dict:
    return {"project_ctry_name": place, "notice_type": "Invitation for Bids",
            "notice_status": "Active", "procurement_method_name": "Request for Bids",
            "submission_deadline_date": "2027-01-01T00:00:00Z"}


def test_no_chosen_markets_means_everywhere(db) -> None:
    """A filter nobody set must never be the reason a tender is invisible."""
    assert db._in_chosen_markets(notice("Kenya"), ())
    assert db._in_chosen_markets(notice("Bangladesh"), ())


@pytest.mark.parametrize(
    ("place", "chosen", "expected"),
    [
        ("Kenya", ("kenya",), True),
        ("Kenya", ("india",), False),
        # A third of notices are filed under a World Bank region, not a country, so a
        # partial match is what makes the filter usable rather than a trap.
        ("Eastern and Southern Africa", ("africa",), True),
        ("South Africa", ("africa",), True),
        ("Gambia, The", ("gambia",), True),
        ("Bangladesh", ("kenya", "bangladesh"), True),
        ("Bangladesh", ("kenya", "india"), False),
        ("", ("kenya",), False),
    ],
)
def test_market_matching(db, place: str, chosen: tuple[str, ...], expected: bool) -> None:
    assert db._in_chosen_markets(notice(place), chosen) is expected


def test_markets_are_applied_before_anything_expensive(db) -> None:
    """The filter runs in the body-free phase, so excluding a market also removes its
    body fetches rather than just its rows."""
    from datetime import date

    kept = db._survives_cheap_filters(
        notice("Kenya"), only_biddable=True, firms_only=True,
        as_of=date(2026, 9, 29), regions=("kenya",))
    dropped = db._survives_cheap_filters(
        notice("Kenya"), only_biddable=True, firms_only=True,
        as_of=date(2026, 9, 29), regions=("india",))
    assert kept and not dropped


def test_a_database_made_before_markets_existed_still_opens(tmp_path, monkeypatch) -> None:
    """Anyone already running the dashboard has a filters table without the column, and
    CREATE TABLE IF NOT EXISTS cannot add one."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE filters (name TEXT PRIMARY KEY, find TEXT, hide TEXT,
               only_biddable INT DEFAULT 1, firms_only INT DEFAULT 1, updated_at TEXT);
           INSERT INTO filters VALUES('default','["SIEM"]','[]',1,1,'2026-09-01');
           PRAGMA user_version = 0;"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("WB_DASHBOARD_DB", str(path))
    from wb_connector import dashboard

    importlib.reload(dashboard)
    with closing(dashboard.connect()) as c:
        flt = dashboard.load_filter(c)
        assert flt["find"] == ["SIEM"], "the existing filter must survive the migration"
        assert flt["regions"] == []


def test_markets_survive_an_unrelated_filter_edit(db) -> None:
    """Every save rewrites the whole row, so a route that forgot to pass the markets
    through would silently clear them."""
    with closing(db.connect()) as conn:
        db.save_filter(conn, ["SIEM"], [], True, True, ["Kenya"])
    from fastapi.testclient import TestClient

    with TestClient(db.build_app(), base_url="http://127.0.0.1:8000") as client:
        client.post("/add", data={"kind": "hide", "term": "Siem Reap"}, follow_redirects=False)
        client.post("/toggle", data={"field": "only_biddable"}, follow_redirects=False)
    with closing(db.connect()) as conn:
        assert db.load_filter(conn)["regions"] == ["Kenya"]


def test_a_market_can_be_added_and_removed_through_the_page(db) -> None:
    from fastapi.testclient import TestClient

    with closing(db.connect()) as conn:
        db.save_filter(conn, ["SIEM"], [], True, True, [])
    with TestClient(db.build_app(), base_url="http://127.0.0.1:8000") as client:
        client.post("/add", data={"kind": "regions", "term": "Kenya"}, follow_redirects=False)
        with closing(db.connect()) as conn:
            assert db.load_filter(conn)["regions"] == ["Kenya"]
        client.post("/remove", data={"kind": "regions", "term": "Kenya"}, follow_redirects=False)
    with closing(db.connect()) as conn:
        # Unlike the search terms, an empty market list is legitimate: it means everywhere.
        assert db.load_filter(conn)["regions"] == []
