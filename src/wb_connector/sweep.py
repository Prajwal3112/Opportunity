"""Product-term sweep: does searching for what we SELL beat searching for the domain?

Measured on the captured corpus, a domain-word search is a poor discovery strategy:
only 28.8% of notices the taxonomy matches contain the literal "cybersecurity", and a
genuine SIEM purchase was found inside a notice titled "Supply and Delivery of IT
Equipment" whose text never says "cyber" at all. The product is usually a line item,
not the subject of the tender.

This runs the comparison against the live service and reports which strategy actually
finds biddable work. It also settles two open questions in the same session:

* Is ``qterm`` phrase-matching or token-OR?  ("information security" returns ~26,044
  against 475 for "cybersecurity" — if that is token-OR the number is meaningless.)
* Does ``/api/procnotices`` honour any date filter? If so, incremental sync gets cheap.

Run from the repository root, on a network that can reach the service::

    python -m wb_connector.sweep
    python -m wb_connector.sweep --rows 200 --delay 1.5
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opportunity_engine.biddability import assess

from .client import WorldBankApiClient
from .config import get_settings
from .connectors import _date as _as_date
from .errors import WorldBankConnectorError
from .models import APPLYABLE_NOTICE_TYPES

PATH = "/api/procnotices"
KEY = "procnotices"
OUT_ROOT = Path("captures")

# What the organisation actually sells. These are the phrases a bill of quantities or a
# technical specification would use -- not the phrases a policy document would.
PRODUCT_TERMS = [
    "SIEM", "security information and event management",
    "SOAR", "security orchestration",
    "DLP", "data loss prevention", "data leakage prevention",
    "anti phishing", "phishing simulation", "secure email gateway",
    "url filtering", "web filtering", "secure web gateway", "content filtering",
    "WAF", "web application firewall", "DDoS protection",
    "UEBA", "user behaviour analytics", "user behavior analytics",
    "log management", "log correlation", "security monitoring",
    "next generation firewall", "endpoint detection and response",
]

# The domain-word baseline this is being compared against.
DOMAIN_TERMS = ["cybersecurity", "cyber security", "cyber", "information security"]

# Terms whose result counts answer the phrase-vs-token question. If `qterm` were
# token-OR, a two-word phrase would return MORE than either word alone, not fewer.
SEMANTICS_PROBES = ["information security", "information", "security"]


def as_int(value: Any) -> int | None:
    """Envelope numbers arrive as JSON strings ("417771"), not integers."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    container = payload.get(KEY)
    if isinstance(container, dict):
        container = list(container.values())
    return [r for r in (container or []) if isinstance(r, dict)]


class Sweep:
    def __init__(self, client: WorldBankApiClient, *, delay: float, budget: int) -> None:
        self.client = client
        self.delay = delay
        self.budget = budget
        self.spent = 0
        self.failures: list[str] = []

    async def get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if self.spent >= self.budget:
            return None
        if self.spent:
            await asyncio.sleep(self.delay)
        self.spent += 1
        try:
            payload, _url = await self.client.get_json(PATH, params)
            return payload
        except WorldBankConnectorError as exc:
            self.failures.append(f"{params}: {type(exc).__name__}: {exc}")
            return None


def _say(text: str) -> None:
    """Print without dying on non-Latin text. Notices arrive in Cyrillic, Greek and CJK,
    and a Windows console is cp1252."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


async def run(*, rows: int, delay: float, budget: int, out: Path) -> None:
    settings = get_settings()
    today = datetime.now(UTC).date()
    findings: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "today": str(today)}

    async with WorldBankApiClient(settings) as client:
        sweep = Sweep(client, delay=delay, budget=budget)

        base = await sweep.get({"rows": 1, "os": 0})
        if base is None:
            _say("Cannot reach the service. Nothing was written.")
            for failure in sweep.failures:
                _say(f"  {failure}")
            return
        total = as_int(base.get("total"))
        _say(f"corpus total: {total:,}\n" if total else "corpus total: unknown\n")
        findings["corpus_total"] = total

        # --- 1. qterm semantics -------------------------------------------------
        _say("[1/4] qterm semantics: phrase match, or token OR?")
        semantics = {}
        for term in SEMANTICS_PROBES:
            payload = await sweep.get({"qterm": term, "rows": 1, "os": 0})
            if payload is not None:
                semantics[term] = as_int(payload.get("total"))
                _say(f"   {term:<24} total={semantics[term]}")
        phrase = None
        if all(t in semantics and semantics[t] is not None for t in SEMANTICS_PROBES):
            pair, left, right = (semantics[t] for t in SEMANTICS_PROBES)
            phrase = pair < min(left, right)
            _say(f"   -> {'PHRASE match' if phrase else 'TOKEN-OR (counts are inflated)'}")
        findings["qterm_semantics"] = {"counts": semantics, "is_phrase_match": phrase}

        # --- 2. domain baseline vs product terms --------------------------------
        async def measure(term: str) -> dict[str, Any]:
            row: dict[str, Any] = {"term": term}
            payload = await sweep.get({"qterm": term, "rows": rows, "os": 0})
            if payload is None:
                return {**row, "total": None}
            row["total"] = as_int(payload.get("total"))
            batch = records(payload)
            live = [r for r in batch if r.get("notice_type") in APPLYABLE_NOTICE_TYPES]
            # Delegates to the shared predicate rather than re-implementing the rule
            # with a lexicographic string-date comparison.
            open_now = [
                r for r in batch
                if assess(
                    notice_type=r.get("notice_type"),
                    notice_status=r.get("notice_status"),
                    procurement_method_name=r.get("procurement_method_name"),
                    submission_deadline_date=_as_date(r.get("submission_deadline_date")),
                    as_of=today,
                ).is_open
            ]
            row["sampled"] = len(batch)
            row["bid_eligible"] = len(live)
            row["open_now"] = len(open_now)
            row["goods"] = sum(1 for r in live if str(r.get("procurement_group") or "") == "GO")
            row["ids_open"] = [r.get("id") for r in open_now]
            row["open_rows"] = [
                {
                    "id": r.get("id"),
                    "deadline": str(r.get("submission_deadline_date") or "")[:10],
                    "type": r.get("notice_type"),
                    "country": r.get("project_ctry_name"),
                    "description": (r.get("bid_description") or "")[:150],
                    "reference": r.get("bid_reference_no"),
                    "contact_email": r.get("contact_email"),
                    "project_id": r.get("project_id"),
                }
                for r in open_now
            ]
            return row

        _say(f"\n[2/4] domain-word baseline (rows={rows})")
        findings["domain"] = []
        for term in DOMAIN_TERMS:
            row = await measure(term)
            findings["domain"].append(row)
            _say(f"   {term:<26} total={row.get('total')!s:>7}  eligible={row.get('bid_eligible')}  open={row.get('open_now')}")

        _say(f"\n[3/4] product-term sweep ({len(PRODUCT_TERMS)} terms)")
        findings["product"] = []
        for term in PRODUCT_TERMS:
            row = await measure(term)
            findings["product"].append(row)
            _say(f"   {term:<40} total={row.get('total')!s:>7}  eligible={row.get('bid_eligible')}  open={row.get('open_now')}")

        # --- 3. date filter probe ------------------------------------------------
        _say("\n[4/4] does /api/procnotices honour a date filter?")
        date_probe = {}
        for param in ("noticedate", "submission_deadline_date_from", "fromdate", "startdate"):
            payload = await sweep.get({"rows": 1, "os": 0, param: "2026-01-01"})
            if payload is None:
                continue
            got = as_int(payload.get("total"))
            verdict = "IGNORED" if got == total else f"HONOURED? total={got}"
            date_probe[param] = verdict
            _say(f"   ?{param}=2026-01-01 -> {verdict}")
        findings["date_filter_probe"] = date_probe

    # --- summary -----------------------------------------------------------------
    domain_open = {i for row in findings["domain"] for i in row.get("ids_open") or []}
    product_open = {i for row in findings["product"] for i in row.get("ids_open") or []}
    only_product = product_open - domain_open
    findings["summary"] = {
        "requests": sweep.spent,
        "open_found_by_domain_terms": len(domain_open),
        "open_found_by_product_terms": len(product_open),
        "open_found_ONLY_by_product_terms": len(only_product),
        "ids_only_product": sorted(only_product),
    }

    _say("\n" + "=" * 66)
    _say(f"  open opportunities found by DOMAIN words   : {len(domain_open)}")
    _say(f"  open opportunities found by PRODUCT words  : {len(product_open)}")
    _say(f"  found ONLY by product words (the gate)     : {len(only_product)}")
    _say("=" * 66)
    _say("\n  GATE 2 DECISION RULE: >= 2 applyable product-shaped opportunities per")
    _say("  month justifies the platform. Fewer means a daily script plus a second source.")

    rows_only = [r for row in findings["product"] for r in row.get("open_rows") or []
                 if r["id"] in only_product]
    if rows_only:
        _say("\n  Invisible to a domain-word search:")
        for r in rows_only[:15]:
            _say(f"   [{r['deadline']}] {r['type']} | {r['country']}")
            _say(f"      {r['description'][:88]}")
            _say(f"      ref={r['reference']}  contact={r['contact_email']}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings, indent=2, default=str), encoding="utf-8")
    _say(f"\n  {sweep.spent} requests. Full findings written to {out}")
    if sweep.failures:
        _say(f"  {len(sweep.failures)} request(s) failed:")
        for failure in sweep.failures[:5]:
            _say(f"   {failure}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=200, help="records sampled per term (default 200)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--budget", type=int, default=120, help="hard ceiling on total requests")
    parser.add_argument("--out", default=None, help="output JSON path")
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    out = Path(args.out) if args.out else OUT_ROOT / f"sweep-{stamp}" / "findings.json"
    asyncio.run(run(rows=args.rows, delay=args.delay, budget=args.budget, out=out))


if __name__ == "__main__":
    main()
