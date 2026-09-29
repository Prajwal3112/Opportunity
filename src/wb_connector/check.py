"""One command that says whether this machine can actually run the dashboard.

Setup failures here are nearly always environmental rather than bugs, and each has a
different fix: a missing extra, an unreadable profile, a read-only directory, or a
network that intercepts TLS. Told apart, each takes a minute to fix; discovered through
a stack trace from a half-loaded page, they look the same as the tool being broken.

Run it with ``python -m wb_connector.check``. Exit code 0 means the dashboard will work,
1 means something is genuinely wrong, and 2 means only the network is blocked -- worth
distinguishing, because the software is fine and the fix is to change networks.
"""
from __future__ import annotations

import sys
from pathlib import Path

EXIT_OK, EXIT_BROKEN, EXIT_NETWORK = 0, 1, 2

# Console encoding on Windows is often cp1252, which cannot render a tick. Losing the
# whole report to a UnicodeEncodeError while diagnosing setup would be its own joke.
PASS, FAIL, WARN = "[ok]", "[!!]", "[--]"


def _say(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def check_python() -> tuple[bool, str]:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    detail = f"Python {major}.{minor} ({sys.executable})"
    return ok, detail if ok else f"{detail} -- 3.11 or newer is required"


def check_web_extra() -> tuple[bool, str]:
    missing = []
    for module, package in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                            ("python_multipart", "python-multipart")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        return False, f"missing {', '.join(missing)} -- run: pip install -e \".[web]\""
    return True, "fastapi, uvicorn and python-multipart are installed"


def check_taxonomy() -> tuple[bool, str]:
    from opportunity_engine.domain.taxonomy import load_taxonomy

    from .dashboard import TAXONOMY_PATH

    path = Path(TAXONOMY_PATH)
    if not path.exists():
        return False, f"no product catalogue at {path} -- set WB_TAXONOMY to point at one"
    try:
        taxonomy = load_taxonomy(path, strict=True)
    except ValueError as exc:
        return False, f"catalogue at {path} failed its own lint: {str(exc)[:160]}"
    except OSError as exc:
        return False, f"cannot read {path}: {exc}"
    return True, f"{len(taxonomy.terms)} terms, version {taxonomy.taxonomy_version}"


def check_writable() -> tuple[bool, str]:
    """The dashboard creates its SQLite file on first run; a read-only directory is a
    common surprise when the checkout sits somewhere managed."""
    from .dashboard import DB_PATH

    path = Path(DB_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"cannot write beside {path}: {exc} -- set WB_DASHBOARD_DB elsewhere"
    return True, f"state file {path}"


def check_service() -> tuple[bool | None, str]:
    """True reachable, None blocked by the network, False the service itself failed."""
    import httpx

    from .config import get_settings

    url = str(get_settings().world_bank_base_url).rstrip("/") + "/api/procnotices"
    try:
        response = httpx.get(url, params={"qterm": "SIEM", "rows": 1, "fl": "id"}, timeout=30)
    except httpx.ConnectError as exc:
        if "CERTIFICATE_VERIFY" in str(exc):
            return None, ("TLS is being intercepted on this network (certificate cannot be "
                          "verified). The software is fine; the network is the problem. "
                          "Try a phone hotspot, or ask IT to exempt search.worldbank.org "
                          "from SSL deep inspection.")
        return None, f"cannot reach search.worldbank.org: {str(exc)[:120]}"
    except httpx.HTTPError as exc:
        return False, f"request failed: {type(exc).__name__}: {str(exc)[:120]}"
    if response.status_code != 200:
        return False, f"service answered {response.status_code}"
    try:
        total = response.json().get("total")
    except ValueError:
        return False, "service answered 200 but not with JSON (a captive portal?)"
    return True, f"reachable -- 'SIEM' matches {total} notices"


def main() -> int:
    _say("\nChecking this machine can run the tender dashboard\n")
    broken = False

    for label, check in (("Python", check_python), ("Web packages", check_web_extra),
                         ("Product catalogue", check_taxonomy), ("Writable state", check_writable)):
        ok, detail = check()
        _say(f"  {PASS if ok else FAIL} {label:18} {detail}")
        broken = broken or not ok

    reachable, detail = check_service()
    mark = PASS if reachable else (WARN if reachable is None else FAIL)
    _say(f"  {mark} {'World Bank API':18} {detail}")

    _say()
    if broken or reachable is False:
        _say("Something is wrong. Fix the [!!] lines above, then run this again.")
        return EXIT_BROKEN
    if reachable is None:
        _say("Everything installed correctly, but this network blocks the World Bank.")
        _say("The dashboard will run and say so; it will simply show no tenders.")
        return EXIT_NETWORK
    _say("All good. Start it with:  python -m wb_connector.dashboard")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
