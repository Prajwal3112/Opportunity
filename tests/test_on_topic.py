"""Tenders FOR the product, versus tenders that merely mention it.

Measured on a live result set: 18 tenders, 2 of which named a matched term in their own
subject line. The other 16 were agricultural quality improvement, 11kV auto reclosers,
freight locomotives and garment skills training. Presenting all 18 as one list of
opportunities is what made the page feel productive and waste a morning.
"""
from __future__ import annotations

from types import SimpleNamespace

from wb_connector.dashboard_template import render_groups


def row(subject: str, *, on_topic: bool, days: int | None = 10):
    notice = SimpleNamespace(
        bid_description=subject, notice_text="", project_country_name="Kenya",
        notice_type="Invitation for Bids", submission_deadline_date=None,
        bid_reference_no="REF-1", contact_email="buyer@example.org", contact_web_url=None,
        external_id="OP1", project_id="P1",
    )
    return SimpleNamespace(notice=notice, why="SIEM", days=days, is_new=False,
                           state="", docs=(), on_topic=on_topic)


def test_on_topic_tenders_lead_and_mentions_are_folded() -> None:
    html = render_groups([
        row("Implementation of a SIEM platform", on_topic=True),
        row("Supply of 45 freight locomotives", on_topic=False),
        row("Agricultural quality improvement", on_topic=False),
    ])
    assert html.index("SIEM platform") < html.index("mentions")
    assert "2 more that only" in html
    assert "freight locomotives" in html, "nothing may be discarded"


def test_nothing_on_topic_says_so_rather_than_leading_with_noise() -> None:
    """The honest answer to a quiet day. Leading with sixteen locomotive tenders would
    read as a pipeline."""
    html = render_groups([row("Supply of 45 freight locomotives", on_topic=False)])
    assert "Nothing today is a tender for your products" in html
    assert "class=\"mentions\"" in html
    assert "freight locomotives" in html


def test_all_on_topic_adds_no_folded_section() -> None:
    html = render_groups([row("Implementation of a SIEM platform", on_topic=True)])
    assert "class=\"mentions\"" not in html
    assert "SIEM platform" in html


def test_both_groups_keep_their_closing_horizons() -> None:
    """The split is on top of the horizons, not instead of them: a mentioned tender
    closing this week still closes this week."""
    html = render_groups([
        row("A SIEM platform", on_topic=True, days=3),
        row("Locomotives", on_topic=False, days=3),
    ])
    assert html.count("Closing within a week") == 2
