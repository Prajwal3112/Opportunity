"""One-session reconnaissance against the live World Bank API.

Run this from a network that can reach ``search.worldbank.org``. It captures raw
responses, then reports what is actually in them. Nothing here infers, fills in,
or repairs a response: every field name and count in the report is read from bytes
the service returned, and every failure is recorded as a failure.

The session answers four questions the repository cannot currently answer:

1. What are the real field names on each endpoint, and how often are they populated?
2. Does pagination behave the way ``client.paginate``/``paginate_offset`` assume?
3. Can cybersecurity filtering be pushed to the server, or must we crawl everything?
4. Are notices actionable -- do they carry a status, a deadline and a reference?

Usage from the repository root::

    python -m wb_connector.recon                 # default session
    python -m wb_connector.recon --pages 40      # capture a larger corpus
    python -m wb_connector.recon --delay 2.0     # be gentler on the service
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import WorldBankApiClient
from .config import get_settings
from .errors import WorldBankConnectorError

CAPTURE_VERSION = 1
CAPTURE_ROOT = Path("captures")

PROCUREMENT = ("/api/procnotices", "procnotices")
PROJECTS = ("/api/v2/projects", "projects")
DOCUMENTS = ("/api/v3/wds", "documents")

# Terms used only to test whether the service filters server-side. This is a
# capability probe, not the product taxonomy.
FILTER_PROBE_TERMS = ("cybersecurity", "SIEM")

# Fields that decide whether a notice can actually be bid on. If these are sparse,
# the product thesis needs rework, so they are reported separately and prominently.
APPLYABILITY_FIELDS = (
    "notice_type",
    "notice_status",
    "submission_date",
    "bid_reference_no",
    "project_id",
)


class Session:
    """Bounded, polite request session that records everything it sends and receives."""

    def __init__(self, client: WorldBankApiClient, *, out_dir: Path, delay: float, budget: int) -> None:
        self.client = client
        self.out_dir = out_dir
        self.delay = delay
        self.budget = budget
        self.spent = 0
        self.log: list[dict[str, Any]] = []

    async def fetch(self, path: str, params: dict[str, Any], *, save_as: str | None = None) -> dict[str, Any] | None:
        """Issue one request. Returns the payload, or None when the request failed."""
        if self.spent >= self.budget:
            print(f"  ! request budget of {self.budget} exhausted; stopping")
            return None
        if self.spent:
            await asyncio.sleep(self.delay)
        self.spent += 1
        label = save_as or f"{path} {params}"
        try:
            payload, url = await self.client.get_json(path, params)
        except WorldBankConnectorError as exc:
            print(f"  x {label}: {type(exc).__name__}: {exc}")
            self.log.append({"path": path, "params": params, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return None
        self.log.append({"path": path, "params": params, "ok": True, "url": url})
        if save_as:
            self._write_capture(save_as, path=path, params=params, url=url, payload=payload)
        print(f"  . {label}")
        return payload

    def _write_capture(self, name: str, *, path: str, params: dict[str, Any], url: str, payload: dict[str, Any]) -> None:
        """Persist the untouched response with the provenance the capture policy requires."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "capture_version": CAPTURE_VERSION,
            "captured_at": datetime.now(UTC).isoformat(),
            "endpoint": path,
            "request_url": url,
            "request_params": params,
            "response": payload,
        }
        (self.out_dir / f"{name}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
        )


def records_of(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Read the record list out of an envelope without assuming its container type."""
    container = payload.get(key)
    if isinstance(container, dict):
        candidates = list(container.values())
    elif isinstance(container, list):
        candidates = container
    else:
        return []
    return [record for record in candidates if isinstance(record, dict)]


def field_report(records: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    """Field name, populated count, and one real sample value, across observed records."""
    present: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for record in records:
        for name, value in record.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            present[name] += 1
            if name not in sample:
                text = value if isinstance(value, str) else json.dumps(value)
                sample[name] = text[:70].replace("\n", " ")
    return [(name, count, sample.get(name, "")) for name, count in present.most_common()]


def envelope_report(payload: dict[str, Any], record_key: str) -> dict[str, Any]:
    return {
        "top_level_keys": sorted(k for k in payload if k != record_key),
        "record_container_type": type(payload.get(record_key)).__name__,
        "counts": {k: payload.get(k) for k in ("total", "rows", "os", "page", "pages") if k in payload},
    }


async def run(*, pages: int, delay: float, budget: int, session_name: str) -> None:
    settings = get_settings()
    out_dir = CAPTURE_ROOT / session_name
    findings: dict[str, Any] = {"session": session_name, "started_at": datetime.now(UTC).isoformat()}

    async with WorldBankApiClient(settings) as client:
        session = Session(client, out_dir=out_dir, delay=delay, budget=budget)

        # --- 1. Reachability -------------------------------------------------
        print("\n[1/6] Reachability")
        preflight = await session.fetch(PROCUREMENT[0], {"rows": 1, "os": 0}, save_as="00-preflight-procurement")
        if preflight is None:
            findings["reachable"] = False
            _write_report(out_dir, findings, session)
            print("\nABORTED: the procurement endpoint did not return JSON. See RECON.md.")
            return
        findings["reachable"] = True

        # --- 2. Envelope and page-size ceiling -------------------------------
        print("\n[2/6] Envelope and page-size ceiling")
        findings["procurement_envelope"] = envelope_report(preflight, PROCUREMENT[1])
        page_sizes: dict[int, int] = {}
        for requested in (100, 500, 1000):
            probe = await session.fetch(PROCUREMENT[0], {"rows": requested, "os": 0})
            if probe is not None:
                page_sizes[requested] = len(records_of(probe, PROCUREMENT[1]))
        findings["page_size_probe"] = page_sizes
        effective_rows = max((got for got in page_sizes.values() if got), default=100)
        print(f"  -> largest page actually returned: {effective_rows} records")

        # --- 3. Procurement corpus + pagination ------------------------------
        print(f"\n[3/6] Procurement corpus ({pages} pages of {effective_rows})")
        notices: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        offsets_advanced = True
        for index in range(pages):
            offset = index * effective_rows
            payload = await session.fetch(
                PROCUREMENT[0], {"rows": effective_rows, "os": offset},
                save_as=f"10-procurement-os{offset:06d}",
            )
            if payload is None:
                break
            batch = records_of(payload, PROCUREMENT[1])
            if not batch:
                break
            batch_ids = {str(r.get("id")) for r in batch if r.get("id") is not None}
            if index and batch_ids and batch_ids <= seen_ids:
                # The service returned a page we have already seen: `os` is not advancing.
                offsets_advanced = False
                print("  ! offset pagination returned a duplicate page; stopping")
                break
            seen_ids |= batch_ids
            notices.extend(batch)
        findings["offset_pagination_advances"] = offsets_advanced
        findings["notices_captured"] = len(notices)
        findings["notices_unique_ids"] = len(seen_ids)
        print(f"  -> {len(notices)} notices, {len(seen_ids)} unique ids")

        # --- 4. Server-side filtering ----------------------------------------
        print("\n[4/6] Server-side filtering probes")
        baseline_total = preflight.get("total")
        probes: dict[str, Any] = {"baseline_total": baseline_total}
        for param in ("qterm", "q", "notice_type"):
            for term in FILTER_PROBE_TERMS[:1]:
                probe = await session.fetch(PROCUREMENT[0], {"rows": 1, "os": 0, param: term})
                if probe is None:
                    probes[f"procnotices:{param}"] = "request failed"
                    continue
                total = probe.get("total")
                if total == baseline_total:
                    verdict = f"IGNORED (total unchanged at {total})"
                elif isinstance(total, int) and 0 < total < (baseline_total or 0):
                    verdict = f"HONOURED (total narrowed to {total})"
                else:
                    verdict = f"inconclusive (total={total})"
                probes[f"procnotices:{param}"] = verdict
                print(f"  -> procnotices ?{param}={term}: {verdict}")
        wds = await session.fetch(DOCUMENTS[0], {"qterm": "cybersecurity", "rows": 5, "os": 0},
                                  save_as="20-documents-qterm-cybersecurity")
        if wds is not None:
            docs = records_of(wds, DOCUMENTS[1])
            probes["wds:qterm"] = f"returned {len(docs)} documents, total={wds.get('total')}"
            findings["document_fields"] = field_report(docs)
            findings["document_envelope"] = envelope_report(wds, DOCUMENTS[1])
        findings["filter_probes"] = probes

        # --- 5. Projects ------------------------------------------------------
        print("\n[5/6] Projects endpoint")
        first = await session.fetch(PROJECTS[0], {"rows": 20, "page": 1}, save_as="30-projects-page1")
        second = await session.fetch(PROJECTS[0], {"rows": 20, "page": 2}, save_as="31-projects-page2")
        if first is not None:
            project_records = records_of(first, PROJECTS[1])
            findings["project_fields"] = field_report(project_records)
            findings["project_envelope"] = envelope_report(first, PROJECTS[1])
            if second is not None:
                ids_1 = {str(r.get("id")) for r in project_records}
                ids_2 = {str(r.get("id")) for r in records_of(second, PROJECTS[1])}
                distinct = bool(ids_2) and not ids_2 <= ids_1
                findings["page_pagination_advances"] = distinct
                print(f"  -> page=2 returns different records: {distinct}")

        # --- 6. Notice analysis ----------------------------------------------
        print("\n[6/6] Notice analysis")
        if notices:
            findings["notice_fields"] = field_report(notices)
            applyability = {}
            for name in APPLYABILITY_FIELDS:
                filled = sum(1 for n in notices if str(n.get(name) or "").strip())
                applyability[name] = {
                    "populated": filled,
                    "of": len(notices),
                    "pct": round(100 * filled / len(notices), 1),
                }
            findings["applyability"] = applyability
            for name in ("notice_type", "notice_status", "procurement_method_name"):
                values = Counter(str(n.get(name) or "(empty)").strip() for n in notices)
                findings[f"values:{name}"] = values.most_common(25)

    _write_report(out_dir, findings, session)
    print(f"\nDone. {session.spent} requests. Captures and RECON.md in {out_dir}")


def _write_report(out_dir: Path, findings: dict[str, Any], session: Session) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        f"# Reconnaissance session: {findings['session']}",
        "",
        f"Started {findings['started_at']}. {session.spent} requests issued.",
        "Every value below was read from a real response. Nothing is inferred.",
        "",
    ]

    if not findings.get("reachable"):
        lines += ["## Endpoint unreachable", "", "The procurement endpoint did not return JSON.",
                  "Request log:", "", "```"]
        lines += [json.dumps(entry, indent=2) for entry in session.log]
        lines += ["```", ""]
        (out_dir / "RECON.md").write_text("\n".join(lines), encoding="utf-8")
        return

    def table(title: str, rows: list[tuple[str, int, str]], total: int) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| field | populated | of | sample value |")
        lines.append("| --- | ---: | ---: | --- |")
        for name, count, sample in rows:
            escaped = sample.replace("|", r"\|")
            lines.append(f"| `{name}` | {count} | {total} | {escaped} |")
        lines.append("")

    app = findings.get("applyability")
    if app:
        lines += ["## Can we actually apply? (the product question)", "",
                  "| field | populated | of | % |", "| --- | ---: | ---: | ---: |"]
        for name, stat in app.items():
            lines.append(f"| `{name}` | {stat['populated']} | {stat['of']} | {stat['pct']}% |")
        lines.append("")

    for key, title in (("notice_type", "Notice types"), ("notice_status", "Notice statuses"),
                       ("procurement_method_name", "Procurement methods")):
        values = findings.get(f"values:{key}")
        if values:
            lines += [f"### {title}", "", "| value | count |", "| --- | ---: |"]
            lines += [f"| {value} | {count} |" for value, count in values]
            lines.append("")

    lines += ["## Pagination", "",
              f"- Offset pagination advances (procnotices): **{findings.get('offset_pagination_advances')}**",
              f"- Page pagination advances (projects): **{findings.get('page_pagination_advances')}**",
              f"- Page-size probe (requested -> returned): `{findings.get('page_size_probe')}`", ""]

    lines += ["## Server-side filtering", "",
              "Determines whether cybersecurity filtering can be pushed to the service",
              "or whether the full notice population must be crawled and filtered locally.", "", "```"]
    lines += [f"{k}: {v}" for k, v in (findings.get("filter_probes") or {}).items()]
    lines += ["```", ""]

    for key, title, count_key in (
        ("notice_fields", "Procurement notice fields", "notices_captured"),
        ("document_fields", "Document (WDS) fields", None),
        ("project_fields", "Project fields", None),
    ):
        rows = findings.get(key)
        if rows:
            table(title, rows, findings.get(count_key or "", 0) or max(c for _, c, _ in rows))

    lines += ["## Envelopes", "", "```json"]
    for key in ("procurement_envelope", "document_envelope", "project_envelope"):
        if findings.get(key):
            lines.append(f"{key} = {json.dumps(findings[key], indent=2)}")
    lines += ["```", ""]

    failures = [entry for entry in session.log if not entry["ok"]]
    if failures:
        lines += ["## Failed requests", "", "```"]
        lines += [json.dumps(entry, indent=2) for entry in failures]
        lines += ["```", ""]

    (out_dir / "RECON.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "findings.json").write_text(json.dumps(findings, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pages", type=int, default=20, help="procurement pages to capture (default 20)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    parser.add_argument("--budget", type=int, default=80, help="hard ceiling on total requests (default 80)")
    parser.add_argument("--name", default=None, help="session directory name under captures/")
    args = parser.parse_args()
    name = args.name or f"recon-{datetime.now(UTC).strftime('%Y%m%d-%H%M')}"
    asyncio.run(run(pages=args.pages, delay=args.delay, budget=args.budget, session_name=name))


if __name__ == "__main__":
    main()
