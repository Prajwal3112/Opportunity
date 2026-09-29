"""The setup check.

Its whole job is to tell environmental failures apart, so the tests are about the
distinctions it draws rather than the words it prints. The important one is the last:
an intercepted TLS connection must not read as a broken install, because the fix is to
change network, not to reinstall anything.
"""
from __future__ import annotations

import httpx
import pytest

from wb_connector import check


def test_python_version_passes_on_a_supported_interpreter() -> None:
    ok, detail = check.check_python()
    assert ok
    assert "Python" in detail


def test_web_extra_is_detected() -> None:
    ok, detail = check.check_web_extra()
    assert ok, detail


def test_catalogue_check_reports_the_term_count(tmp_path, monkeypatch) -> None:
    ok, detail = check.check_taxonomy()
    assert ok, detail
    assert "terms" in detail


def test_a_missing_catalogue_is_reported_not_raised(tmp_path, monkeypatch) -> None:
    import importlib

    monkeypatch.setenv("WB_TAXONOMY", str(tmp_path / "absent.json"))
    from wb_connector import dashboard

    importlib.reload(dashboard)
    ok, detail = check.check_taxonomy()
    assert not ok
    assert "WB_TAXONOMY" in detail


def test_writable_state_passes_in_a_temp_directory(tmp_path, monkeypatch) -> None:
    import importlib

    monkeypatch.setenv("WB_DASHBOARD_DB", str(tmp_path / "state.db"))
    from wb_connector import dashboard

    importlib.reload(dashboard)
    ok, detail = check.check_writable()
    assert ok, detail
    assert not (tmp_path / ".write-probe").exists(), "the probe file must be cleaned up"


def test_intercepted_tls_is_a_network_verdict_not_a_failure(monkeypatch) -> None:
    """Exit code 2, not 1. The install is fine; the network is the problem, and telling
    a user to reinstall when they need a different network wastes their afternoon."""

    def intercepted(*args, **kwargs):
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate"
        )

    monkeypatch.setattr(httpx, "get", intercepted)
    reachable, detail = check.check_service()
    assert reachable is None
    assert "network" in detail.lower()


def test_a_reachable_service_passes(monkeypatch) -> None:
    def ok(*args, **kwargs):
        return httpx.Response(200, json={"total": 213, "procnotices": []},
                              request=httpx.Request("GET", "https://search.worldbank.org"))

    monkeypatch.setattr(httpx, "get", ok)
    reachable, detail = check.check_service()
    assert reachable is True
    assert "213" in detail


def test_a_captive_portal_is_a_failure_not_a_pass(monkeypatch) -> None:
    """A hotel or campus portal answers 200 with HTML. Treating that as success would
    send the user to a dashboard that silently finds nothing."""

    def portal(*args, **kwargs):
        return httpx.Response(200, text="<html>Sign in to continue</html>",
                              request=httpx.Request("GET", "https://search.worldbank.org"))

    monkeypatch.setattr(httpx, "get", portal)
    reachable, detail = check.check_service()
    assert reachable is False
    assert "JSON" in detail


@pytest.mark.parametrize("code", [500, 503])
def test_a_failing_service_is_reported(monkeypatch, code: int) -> None:
    def bad(*args, **kwargs):
        return httpx.Response(code, request=httpx.Request("GET", "https://search.worldbank.org"))

    monkeypatch.setattr(httpx, "get", bad)
    reachable, detail = check.check_service()
    assert reachable is False
    assert str(code) in detail
