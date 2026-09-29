"""Scheduled digest: run the saved filter, report only what changed.

The dashboard answers "what is open right now". This answers "what should I look at",
which is the question that survives not opening a browser. With a 15-day median
publication-to-deadline window and p10 of zero days, a page you forget to open loses more
than a mail you cannot miss.

It reports three things, and stays silent otherwise:

* a matching notice that has never been **delivered** to a human;
* something marked PURSUING whose deadline falls inside the reminder window;
* something marked PURSUING that has **changed** -- cancelled, re-dated, or vanished
  from the results. A cancelled tender you are actively bidding is the most important
  message this tool can send, and it must never read as a quiet week.

Delivery accounting is deliberate. ``delivered`` is written only after a channel confirms,
so a failed send, a crash, or a laptop asleep at the scheduled hour leaves the notice
eligible for the next run instead of burning it. Running without ``--send`` is a preview:
it prints and records nothing.

Run it::

    python -m wb_connector.digest               # preview, records nothing
    python -m wb_connector.digest --send        # email, and record on confirmed send

Exit codes: 0 nothing to report, 3 reported, 1 delivery failed, 2 service unreachable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import smtplib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from .dashboard import Result, connect, delivered_ids, load_filter, mark_delivered, search

REMINDER_WINDOW_DAYS = 7

EXIT_NOTHING, EXIT_SEND_FAILED, EXIT_UNREACHABLE, EXIT_REPORTED = 0, 1, 2, 3


def pursued(conn: sqlite3.Connection) -> dict[str, dict | None]:
    """PURSUING notices mapped to their stored payload, if one was captured."""
    out: dict[str, dict | None] = {}
    for row in conn.execute(
        "SELECT notice_id, raw_payload FROM notice_state WHERE state='PURSUING'"
    ):
        try:
            out[row["notice_id"]] = json.loads(row["raw_payload"]) if row["raw_payload"] else None
        except ValueError:
            out[row["notice_id"]] = None
    return out


def dismissed(conn: sqlite3.Connection) -> set[str]:
    return {row["notice_id"] for row in
            conn.execute("SELECT notice_id FROM notice_state WHERE state='DISMISSED'")}


def changes_on_pursued(
    stored: dict[str, dict | None], rows: list[Result]
) -> list[tuple[str, str, str]]:
    """What happened to the bids the user is actively working.

    Compares the stored snapshot against what came back. Without this, a cancelled tender
    simply leaves the result set and the digest prints "nothing new" -- silence for the
    single most consequential event in the product.
    """
    live = {r.notice.external_id: r.notice for r in rows}
    events: list[tuple[str, str, str]] = []
    for notice_id, snapshot in stored.items():
        title = (snapshot or {}).get("bid_description") or notice_id
        notice = live.get(notice_id)
        if notice is None:
            gone = ("no longer in your results - it may be cancelled, closed, or your "
                    "filter no longer matches it. Check directly.")
            events.append((notice_id, title, gone))
            continue
        was_status = (snapshot or {}).get("notice_status")
        if was_status and notice.notice_status and notice.notice_status != was_status:
            events.append((notice_id, title, f"status changed: {was_status} -> {notice.notice_status}"))
        was_deadline = str((snapshot or {}).get("submission_deadline_date") or "")[:10]
        now_deadline = str(notice.submission_deadline_date or "")
        if was_deadline and now_deadline and was_deadline != now_deadline:
            events.append((notice_id, title, f"deadline moved: {was_deadline} -> {now_deadline}"))
    return events


def render_text(new: list[Result], closing_soon: list[Result],
                changed: list[tuple[str, str, str]]) -> str:
    lines: list[str] = []
    if changed:
        lines.append(f"{len(changed)} CHANGE{'S' if len(changed) != 1 else ''} to what you are pursuing")
        lines.append("")
        for _id, title, what in changed:
            lines.append(f"  {title}")
            lines.append(f"    {what}")
            lines.append("")
    if new:
        lines.append(f"{len(new)} new opportunit{'y' if len(new) == 1 else 'ies'}")
        lines.append("")
        for r in new:
            if r.days is None:
                when = "no deadline published - check the bid document"
            else:
                when = f"closes in {r.days} days"
            lines.append(f"  {r.notice.bid_description or '(untitled)'}")
            lines.append(f"    {when}  |  {r.notice.project_country_name or '-'}"
                         f"  |  {r.notice.notice_type or '-'}")
            if r.why:
                lines.append(f'    mentions "{r.why}"')
            if r.notice.contact_email:
                lines.append(f"    contact: {r.notice.contact_email}"
                             f"  ref: {r.notice.bid_reference_no or '-'}")
            lines.append("")
    if closing_soon:
        lines.append(f"{len(closing_soon)} you are pursuing clos"
                     f"{'es' if len(closing_soon) == 1 else 'e'} soon")
        lines.append("")
        for r in closing_soon:
            lines.append(f"  {r.days}d  |  {r.notice.bid_description or '(untitled)'}")
            lines.append(f"    ref: {r.notice.bid_reference_no or '-'}"
                         f"  contact: {r.notice.contact_email or '-'}")
            lines.append("")
    lines.append("Dashboard: http://127.0.0.1:8000")
    return "\n".join(lines)


def subject_line(new: list[Result], closing_soon: list[Result],
                 changed: list[tuple[str, str, str]]) -> str:
    parts = []
    if changed:
        parts.append(f"{len(changed)} changed")
    if new:
        parts.append(f"{len(new)} new")
    if closing_soon:
        soonest = min(r.days for r in closing_soon if r.days is not None)
        parts.append(f"{len(closing_soon)} closing (next in {soonest}d)")
    return "Opportunities: " + ", ".join(parts)


def send_email(subject: str, body: str) -> tuple[bool, str]:
    """Send via SMTP configured purely through the environment.

    Returns ``(delivered, explanation)`` rather than raising: the digest must still print
    when mail cannot go out. The caller branches on the boolean -- which is the whole
    point, because treating "printed but not sent" as delivered is how notices were lost.
    """
    host = os.environ.get("DIGEST_SMTP_HOST")
    to = os.environ.get("DIGEST_TO")
    sender = os.environ.get("DIGEST_FROM", to or "")
    if not (host and to):
        return False, "not sent: set DIGEST_SMTP_HOST and DIGEST_TO to enable email"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message.set_content(body)
    port = int(os.environ.get("DIGEST_SMTP_PORT", "587"))
    user = os.environ.get("DIGEST_SMTP_USER")
    password = os.environ.get("DIGEST_SMTP_PASSWORD")
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"not sent: {type(exc).__name__}: {exc}"
    return True, f"sent to {to}"


async def run(*, send: bool, window: int) -> int:
    with closing(connect()) as conn:
        flt = load_filter(conn)
        already_delivered = delivered_ids(conn)
        chasing = pursued(conn)
        skip = dismissed(conn)

    # record_seen=False: the digest must not write any ledger before it has delivered.
    rows, totals, warnings = await search(
        flt["find"], flt["hide"],
        only_biddable=flt["only_biddable"], firms_only=flt["firms_only"],
        record_seen=False,
    )

    unreachable = [term for term, total in totals.items() if total is None]
    new = [r for r in rows
           if r.notice.external_id not in already_delivered
           and r.notice.external_id not in skip]
    closing_soon = [r for r in rows
                    if r.notice.external_id in chasing
                    and r.days is not None and 0 <= r.days <= window]
    changed = changes_on_pursued(chasing, rows)

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    for warning in warnings:
        print(f"  ! {warning}")

    if not new and not closing_soon and not changed:
        # An unreachable term is never silence. Any failure -- not only a total blackout --
        # makes "nothing new" a claim this run cannot support.
        if unreachable:
            print(f"[{stamp}] {len(unreachable)} of {len(totals)} terms UNREACHABLE "
                  f"({', '.join(unreachable[:4])}) - this is NOT 'nothing new'.")
            return EXIT_UNREACHABLE
        print(f"[{stamp}] nothing new. {len(rows)} still open, no reminders due. No mail sent.")
        return EXIT_NOTHING

    body = render_text(new, closing_soon, changed)
    subject = subject_line(new, closing_soon, changed)
    print(f"[{stamp}] {subject}\n")
    print(body)
    if unreachable:
        print(f"\n  ! {len(unreachable)} term(s) unreachable this run; results are partial.")

    if not send:
        print("\n  preview only - nothing recorded as delivered. Use --send to email.")
        return EXIT_REPORTED

    ok, explanation = send_email(subject, body)
    print(f"\n  email: {explanation}")
    if not ok:
        print("  nothing recorded as delivered; these will be reported again next run.")
        return EXIT_SEND_FAILED

    with closing(connect()) as conn:
        mark_delivered(conn, [r.notice.external_id for r in new], "email")
    return EXIT_REPORTED


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true",
                        help="email the digest and record delivery (needs DIGEST_* env vars)")
    parser.add_argument("--window", type=int, default=REMINDER_WINDOW_DAYS,
                        help=f"remind about pursued items closing within N days (default {REMINDER_WINDOW_DAYS})")
    parser.add_argument("--db", default=None, help="path to dashboard.db")
    args = parser.parse_args()
    if args.db:
        from . import dashboard
        dashboard.DB_PATH = Path(args.db).resolve()
    raise SystemExit(asyncio.run(run(send=args.send, window=args.window)))


if __name__ == "__main__":
    main()
