"""Which Host headers the dashboard answers to.

Binding to loopback is not protection on its own. With any Host accepted, a page the
user visits can point a hostname it controls at a private address and become same-origin
with this app -- DNS rebinding -- and then drive every POST on it, including the ones
that rewrite the product catalogue.

The rule splits on whether the Host is a literal address or a name, because rebinding
always arrives as a name.
"""
from __future__ import annotations

import pytest

from wb_connector.dashboard import host_is_allowed


@pytest.mark.parametrize(
    "header",
    [
        "127.0.0.1:8000",
        "localhost",
        "localhost:8000",
        "192.168.43.17:8000",   # A phone hotspot reassigns this on every connection.
        "10.200.10.107:8000",   # Office wifi.
        "172.28.0.1",           # WSL / Docker bridge.
        "169.254.5.5",          # Link-local, when DHCP has not answered yet.
        "[::1]:8000",
        "[fd00::1]:8000",
        "[fe80::1]",
    ],
)
def test_private_and_loopback_addresses_are_served(header: str) -> None:
    assert host_is_allowed(header)


@pytest.mark.parametrize(
    ("header", "why"),
    [
        ("evil.example.com", "a bare attacker hostname"),
        ("evil.example.com:8000", "the same with a port"),
        ("127.0.0.1.nip.io", "a hostname that RESOLVES to loopback - the rebinding shape"),
        ("8.8.8.8", "a public address"),
        ("8.8.8.8:8000", "a public address with a port"),
        ("[2001:4860:4860::8888]:8000", "a public IPv6 address"),
        ("", "an empty header"),
        (None, "no header at all"),
    ],
)
def test_names_and_public_addresses_are_refused(header: str | None, why: str) -> None:
    assert not host_is_allowed(header), why


def test_a_hostname_can_be_allowed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serving this under a real hostname on a server is the one case that needs the
    environment variable; nothing else should require it."""
    assert not host_is_allowed("tenders.internal")
    monkeypatch.setenv("WB_ALLOWED_HOSTS", "tenders.internal, other.internal")
    assert host_is_allowed("tenders.internal")
    assert host_is_allowed("TENDERS.INTERNAL:8000")
    assert host_is_allowed("other.internal")
    assert not host_is_allowed("nope.internal")


def test_the_middleware_refuses_a_bad_host_end_to_end() -> None:
    from fastapi.testclient import TestClient

    from wb_connector.dashboard import build_app

    with TestClient(build_app()) as client:
        response = client.get("/", headers={"Host": "evil.example.com"})
    assert response.status_code == 400
    # The message has to tell the operator what to do, or a server deployment behind a
    # hostname looks like the app is simply broken.
    assert "WB_ALLOWED_HOSTS" in response.text
