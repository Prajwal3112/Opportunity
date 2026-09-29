"""Route behaviour, written against bugs that were actually reachable from the page.

Every test here corresponds to something a user could do with a mouse, or something a
stray request could do to their saved state. The dashboard has no login, so "nobody would
send that" is not a defence.
"""
from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient as _TestClient


def TestClient(app):
    """The default client sends `Host: testserver`, which this app refuses by design.

    That refusal is the DNS-rebinding guard doing its job, so the fix belongs here rather
    than in the app: tests speak to it on loopback, exactly as a browser does.
    """
    return _TestClient(app, base_url="http://127.0.0.1:8000")

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "cybersecurity.json"


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A dashboard with its own database and its own copy of the catalogue.

    No network: every test here drives routes that do not search, or asserts on stored
    state rather than on results.
    """
    profile = tmp_path / "cyber.json"
    shutil.copy(PROFILE, profile)
    monkeypatch.setenv("WB_DASHBOARD_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("WB_TAXONOMY", str(profile))

    from wb_connector import dashboard

    importlib.reload(dashboard)
    return dashboard, profile


def saved_filter(dashboard) -> dict:
    conn = sqlite3.connect(dashboard.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT find, hide FROM filters WHERE name='default'").fetchone()
        return {"find": json.loads(row["find"]), "hide": json.loads(row["hide"])}
    finally:
        conn.close()


def seed_filter(dashboard, find: list[str], hide: list[str]) -> None:
    from contextlib import closing

    with closing(dashboard.connect()) as conn:
        dashboard.save_filter(conn, find, hide, True, True)


def test_an_unknown_term_list_is_refused_not_steered_into_the_exclude_list(app) -> None:
    """`key = "find" if kind == "find" else "hide"` meant any value but "find" wrote to
    the exclude list, and the route answered 303 as though it had worked. A single stray
    exclusion empties the whole page."""
    dashboard, _ = app
    seed_filter(dashboard, ["SIEM"], [])
    with TestClient(dashboard.build_app()) as client:
        response = client.post("/add", data={"kind": "evil", "term": "POISON"},
                               follow_redirects=False)
    assert response.status_code == 422
    assert saved_filter(dashboard)["hide"] == []


@pytest.mark.parametrize("kind", ["find", "hide"])
def test_a_term_can_be_added_and_removed_from_either_list(app, kind: str) -> None:
    dashboard, _ = app
    seed_filter(dashboard, ["SIEM"], [])
    with TestClient(dashboard.build_app()) as client:
        client.post("/add", data={"kind": kind, "term": "  spaced   out  "},
                    follow_redirects=False)
        # Whitespace is collapsed, so the same phrase cannot be stored twice.
        assert "spaced out" in saved_filter(dashboard)[kind]
        client.post("/add", data={"kind": kind, "term": "spaced out"}, follow_redirects=False)
        assert saved_filter(dashboard)[kind].count("spaced out") == 1
        client.post("/remove", data={"kind": kind, "term": "spaced out"},
                    follow_redirects=False)
    assert "spaced out" not in saved_filter(dashboard)[kind]


def test_the_removed_switch_cannot_be_toggled_through_the_route(app) -> None:
    """`firms_only` stopped being a switch because it could not change the list. The
    route must not still accept it, or state drifts from what the page can show."""
    dashboard, _ = app
    seed_filter(dashboard, ["SIEM"], [])
    before = saved_filter(dashboard)
    with TestClient(dashboard.build_app()) as client:
        client.post("/toggle", data={"field": "firms_only"}, follow_redirects=False)
        client.post("/toggle", data={"field": "../../etc/passwd"}, follow_redirects=False)
    assert saved_filter(dashboard) == before


def test_the_real_switch_toggles(app) -> None:
    from contextlib import closing

    dashboard, _ = app
    with TestClient(dashboard.build_app()) as client, closing(dashboard.connect()) as conn:
        start = dashboard.load_filter(conn)["only_biddable"]
        client.post("/toggle", data={"field": "only_biddable"}, follow_redirects=False)
        with closing(dashboard.connect()) as c2:
            assert dashboard.load_filter(c2)["only_biddable"] is not start


def test_an_empty_product_name_gets_a_message_not_a_validation_page(app) -> None:
    """`Form(...)` rejected the empty string before the handler ran, so pressing Add on an
    empty box produced a raw 422 JSON page and the friendly message was unreachable."""
    dashboard, _ = app
    with TestClient(dashboard.build_app()) as client:
        response = client.post("/catalogue/add", data={"text": ""}, follow_redirects=False)
    assert response.status_code == 303
    assert "problem=" in response.headers["location"]
    assert "Type%20the%20product%20name" in response.headers["location"]


def test_adding_a_product_also_searches_for_it_and_removing_it_stops(app) -> None:
    """Asymmetry here left the user with results for a product they had just deleted."""
    dashboard, profile = app
    seed_filter(dashboard, ["SIEM"], [])
    with TestClient(dashboard.build_app()) as client:
        client.post("/catalogue/add", data={"text": "Zero Trust Network Access"},
                    follow_redirects=False)
        assert "Zero Trust Network Access" in saved_filter(dashboard)["find"]
        assert any(t["id"] == "product_zero_trust_network_access"
                   for t in json.loads(profile.read_text(encoding="utf-8"))["terms"])

        client.post("/catalogue/remove", data={"term_id": "product_zero_trust_network_access"},
                    follow_redirects=False)
    assert "Zero Trust Network Access" not in saved_filter(dashboard)["find"]


def test_a_rejected_product_leaves_the_catalogue_untouched(app) -> None:
    dashboard, profile = app
    before = profile.read_bytes()
    with TestClient(dashboard.build_app()) as client:
        response = client.post("/catalogue/add", data={"text": "data(loss)prevention"},
                               follow_redirects=False)
    assert response.status_code == 303
    assert "problem=" in response.headers["location"]
    assert profile.read_bytes() == before


def test_removing_the_last_search_term_is_refused(app) -> None:
    """An empty find list fetches nothing at all, which reads as the tool having broken
    rather than as a filter the user chose."""
    dashboard, _ = app
    seed_filter(dashboard, ["SIEM"], [])
    with TestClient(dashboard.build_app()) as client:
        client.post("/remove", data={"kind": "find", "term": "SIEM"}, follow_redirects=False)
    assert saved_filter(dashboard)["find"], "the last search term must not be removable"


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("http://evil.example.com/x.pdf", 400),
        ("file:///C:/Windows/win.ini", 400),
        ("http://127.0.0.1:8000/", 400),
    ],
)
def test_the_document_proxy_refuses_anything_off_the_allowlist(app, url: str, status: int) -> None:
    """The one route that fetches a caller-supplied URL. A refusal is the caller's fault,
    so it answers 400; 502 would suggest the World Bank had failed."""
    dashboard, _ = app
    with TestClient(dashboard.build_app()) as client:
        response = client.get("/document", params={"url": url})
    assert response.status_code == status
    assert "not allowed" in response.text
