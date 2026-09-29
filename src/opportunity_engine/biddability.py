"""The single owner of "can a firm still bid on this?".

This rule previously existed in five places — ``ProcurementNotice.is_applyable``,
``CandidateEngine._applyable``, the dashboard's filter loop, ``sweep.py``, and prose in
the README — and had already diverged in two directions: the engine omitted the
individual-consultant clause (7 of 40 real records disagreed, all of them job adverts),
and the README documented the weaker version. Every one of those call sites now delegates
here.

It lives at the top of ``opportunity_engine`` rather than in ``domain/`` on purpose:
``domain/__init__`` imports ``adapters``, which imports ``wb_connector.models``, so a
predicate placed there could not be imported *by* ``wb_connector.models`` without a cycle.
``opportunity_engine/__init__`` is a docstring only, so this module is importable from
either side.

**Three states, not two.** A notice with no published deadline is not a closed notice —
its deadline lives in the attached bid document. Measured on real captures, 1,063 of 4,361
bid-eligible notices (24.4%) publish no deadline: Invitation for Bids 21.2%, Request for
Expression of Interest 26.3%, Prequalification 65.5%. Collapsing that into "closed" made a
quarter of the addressable market unconditionally invisible, and the empty state then
claimed "none is currently accepting bids" — a claim the data cannot support.
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Biddability(StrEnum):
    """Why a notice can or cannot be bid, as a value rather than a boolean."""

    BIDDABLE = "BIDDABLE"                    # right type, open, deadline still ahead
    DEADLINE_UNKNOWN = "DEADLINE_UNKNOWN"    # right type, open, no deadline published
    CLOSED = "CLOSED"                        # the published deadline has passed
    WRONG_TYPE = "WRONG_TYPE"                # award, advance notice: nothing to bid
    INDIVIDUAL_ONLY = "INDIVIDUAL_ONLY"      # selects a named person, not a firm
    WITHDRAWN = "WITHDRAWN"                  # cancelled or still a draft

    @property
    def is_open(self) -> bool:
        """True when a firm could still respond.

        DEADLINE_UNKNOWN counts as open: the deadline is in the bid document, and treating
        absence as expiry is what hid 24.4% of the population.
        """
        return self in (Biddability.BIDDABLE, Biddability.DEADLINE_UNKNOWN)


class BiddabilityPolicy(BaseModel):
    """Which vocabulary counts as biddable. Data, so a new domain can replace it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applyable_notice_types: frozenset[str] = Field(
        default=frozenset({
            "Invitation for Bids",
            "Request for Expression of Interest",
            "Invitation for Prequalification",
        })
    )
    # Methods that select a named individual. A company cannot bid these, and in the
    # captured corpus every "cyber" notice using one was a job advert.
    individual_only_methods: frozenset[str] = Field(
        default=frozenset({"individual consultant selection"})
    )
    dead_statuses: frozenset[str] = Field(default=frozenset({"cancelled", "draft"}))


DEFAULT_POLICY = BiddabilityPolicy()


def _norm(value: str | None) -> str:
    """Casefold and collapse internal whitespace.

    ``.strip().casefold()`` alone left internal runs intact, and the API demonstrably
    emits irregular spacing — ``'Consultant Qualification  Selection'`` appears in the
    captures with a double space.
    """
    return " ".join((value or "").split()).casefold()


def assess(
    *,
    notice_type: str | None,
    notice_status: str | None,
    procurement_method_name: str | None,
    submission_deadline_date: date | None,
    as_of: date,
    policy: BiddabilityPolicy = DEFAULT_POLICY,
) -> Biddability:
    """Classify one notice. ``as_of`` is required: no implicit clock.

    Taking the date as an argument rather than reading ``now()`` inside is what makes an
    evaluation reproducible and lets the captures be replayed at a historical date.
    """
    if _norm(notice_type) not in {_norm(t) for t in policy.applyable_notice_types}:
        return Biddability.WRONG_TYPE
    if _norm(notice_status) in {_norm(s) for s in policy.dead_statuses}:
        return Biddability.WITHDRAWN
    if _norm(procurement_method_name) in {_norm(m) for m in policy.individual_only_methods}:
        return Biddability.INDIVIDUAL_ONLY
    if submission_deadline_date is None:
        return Biddability.DEADLINE_UNKNOWN
    return Biddability.BIDDABLE if submission_deadline_date >= as_of else Biddability.CLOSED
