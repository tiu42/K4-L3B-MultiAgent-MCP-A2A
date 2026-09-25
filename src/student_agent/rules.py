"""Deterministic analysis: shipment/payment verdicts, issue candidates and policy parameters.

Rules only propose candidates; the adjudicator decides whether they are conclusive or
whether the case needs LLM adjudication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Any

from .facts import (
    CARRIER,
    DELIVERED,
    ESTIMATED,
    LIMIT,
    Incident,
    eligible_incidents,
    parse_dt,
    pick,
)

PRIMARY_ISSUES = (
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed",
    "unsupported_claim", "insufficient_evidence",
)
# Findings that mean "no problem found" rather than a complaint-worthy issue.
BENIGN_ISSUES = frozenset({"valid_split_payment"})
REFUND_OK = frozenset({"completed", "succeeded", "refunded", "processed", "confirmed"})
REFUND_FAILED = frozenset({"failed", "rejected", "declined", "error"})
REFUND_OPEN = frozenset({"pending", "requested", "processing", "open", "in_progress"})
MISMATCH_KINDS = ("mismatch", "discrepan")
DUPLICATE_WINDOW_HOURS = 24
MAX_SPLIT_PARTS = 4
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Candidate:
    issue: str
    reason: str
    domains: tuple[str, ...]


@dataclass
class ShipmentAnalysis:
    verdict: str
    late_seller_ids: list[str]
    timeline_complete: bool
    late: bool = False


@dataclass
class PaymentAnalysis:
    verdict: str
    captured: Decimal | None
    refunded: Decimal | None
    refundable: Decimal | None
    duplicate_amount: Decimal | None = None
    split: bool = False
    mismatch: bool = False
    refund_state: str | None = None  # failed | pending | refunded


@dataclass
class PolicyRule:
    case_status: str
    recommended_action: str | None
    refund_brl: Decimal | None
    responsible_parties: list[dict[str, Any]]


@dataclass
class PolicyParams:
    version: str | None
    rules: dict[str, PolicyRule] = field(default_factory=dict)
    source: str = "structured"  # structured | llm | missing


def analyze_shipment(incident: Incident, as_of: datetime | None = None) -> ShipmentAnalysis:
    """Shipment verdict as observable at ``as_of`` (the complaint time)."""
    purchased = incident.purchased_at
    carrier = incident.when(CARRIER)
    delivered = incident.when(DELIVERED)
    estimated = incident.when(ESTIMATED)
    status = incident.status or ""
    complete = all(x is not None for x in (purchased, carrier, delivered, estimated))

    late_sellers = []
    for item in incident.items:
        limit = parse_dt(pick(item, LIMIT))
        if carrier and limit and carrier > limit and item.get("seller_id"):
            late_sellers.append(str(item["seller_id"]))
    late_sellers = list(dict.fromkeys(late_sellers))

    late_events = [e for e in incident.shipment_events if "late" in e.kind]
    late = bool(delivered and estimated and delivered > estimated) or bool(late_events)
    # A delivery cannot be complained about as late before its promised date has passed.
    if as_of is not None and estimated is not None and estimated >= as_of:
        late = False
    if status in {"canceled", "cancelled", "unavailable"} or delivered is None:
        verdict = "lost" if status == "lost" else "insufficient_evidence"
        return ShipmentAnalysis(verdict, [], False, False)
    if not late:
        return ShipmentAnalysis("on_time", [], complete, False)
    event_actor = next((e.actor for e in late_events if e.actor), None)
    if late_sellers or event_actor == "seller":
        sellers = late_sellers or incident.seller_ids
        return ShipmentAnalysis("seller_delay", sellers, complete, True)
    return ShipmentAnalysis("logistics_delay", [], complete, True)


def _split_parts(captures: list, order_total: Decimal | None) -> list:
    """Smallest group of >=2 captures that together pay exactly one order total."""
    if order_total is None:
        return []
    for size in range(2, min(len(captures), MAX_SPLIT_PARTS) + 1):
        for group in combinations(captures, size):
            if abs(sum((c.amount for c in group), Decimal("0.00")) - order_total) <= CENT:
                return list(group)
    return []


def analyze_payment(
    incident: Incident, have_payment_evidence: bool, as_of: datetime | None = None
) -> PaymentAnalysis:
    """Payment verdict as observable at ``as_of`` (later refund events are ignored)."""
    if not have_payment_evidence:
        return PaymentAnalysis("insufficient_evidence", None, None, None)
    captures = incident.captures
    refunds = [r for r in incident.refund_events if as_of is None or r.at <= as_of]
    captured = sum((c.amount for c in captures), Decimal("0.00"))
    refunded = sum((r.amount for r in refunds if r.status in REFUND_OK), Decimal("0.00"))
    refundable = max(Decimal("0.00"), captured - refunded)
    result = PaymentAnalysis("reconciled", captured, refunded, refundable)

    total = incident.item_total
    order_total = total / incident.multiplicity if total is not None else None
    split = _split_parts(captures, order_total)
    result.split = bool(split)
    split_ids = {id(c) for c in split}
    by_amount: dict[Decimal, list] = {}
    for c in captures:
        if id(c) not in split_ids:  # parts of a valid split are not duplicates of each other
            by_amount.setdefault(c.amount, []).append(c)
    for amount, group in by_amount.items():
        group.sort(key=lambda e: e.at)
        close = any(
            (b.at - a.at).total_seconds() <= DUPLICATE_WINDOW_HOURS * 3600
            for a, b in zip(group, group[1:], strict=False)
        )
        if len(group) >= 2 and close:
            result.duplicate_amount = amount
    result.mismatch = any(
        any(k in e.kind for k in MISMATCH_KINDS) and e.status not in {"resolved", "closed"}
        for e in incident.payment_events
    )
    failed = [r for r in refunds if r.status in REFUND_FAILED]
    open_ = [r for r in refunds if r.status in REFUND_OPEN]
    if failed:
        result.refund_state, result.verdict = "failed", "refund_failed"
    elif open_:
        result.refund_state, result.verdict = "pending", "refund_pending"
    elif result.duplicate_amount is not None:
        result.verdict = "duplicate_capture"
    elif result.mismatch:
        result.verdict = "capture_mismatch"
    elif refunded > 0:
        result.refund_state, result.verdict = "refunded", "refunded"
    return result


def issue_candidates(
    incident: Incident, shipment: ShipmentAnalysis, payment: PaymentAnalysis
) -> list[Candidate]:
    found: list[Candidate] = []
    status = incident.status or ""
    paid = payment.captured is not None and payment.captured > 0
    if payment.refund_state == "failed":
        found.append(Candidate("refund_failed", "REFUND_EVENT_FAILED", ("payment", "refund")))
    if payment.refund_state == "pending":
        found.append(Candidate("refund_pending", "REFUND_EVENT_OPEN", ("payment", "refund")))
    if payment.duplicate_amount is not None:
        found.append(Candidate("duplicate_charge", "REPEATED_CAPTURE", ("payment",)))
    if payment.mismatch:
        found.append(Candidate("payment_mismatch", "RECONCILIATION_MISMATCH", ("payment",)))
    if status in {"canceled", "cancelled"} and paid:
        found.append(Candidate("canceled_order_paid", "CANCELED_WITH_CAPTURE",
                               ("order", "payment")))
    if status == "unavailable" and paid:
        found.append(Candidate("unavailable_order_paid", "UNAVAILABLE_WITH_CAPTURE",
                               ("order", "payment", "item")))
    if shipment.verdict == "seller_delay":
        found.append(Candidate("late_delivery_seller", "SELLER_MISSED_HANDOFF_LIMIT",
                               ("shipment", "item")))
    if shipment.verdict == "logistics_delay":
        found.append(Candidate("late_delivery_logistics", "CARRIER_DELIVERED_LATE",
                               ("shipment", "item")))
    # A split is a finding on its own when nothing else is wrong, or when merged purchases
    # make it impossible to tell which records the complaint is about.
    if payment.split and (not found or incident.multiplicity > 1):
        found.append(Candidate("valid_split_payment", "SPLIT_SUMS_TO_ORDER_TOTAL", ("payment",)))
    return found


def choose_incident(
    incidents: list[Incident], opened_at: datetime | None, claimed_issues: list[str],
    have_payment_evidence: bool,
) -> Incident | None:
    """Pick the purchase the complaint is about.

    Among purchases made before the complaint (newest first), prefer the newest one whose
    evidence, as observable when the complaint was opened, shows the issue the customer
    raised. Without a usable claim, prefer the newest purchase showing any problem. Otherwise
    fall back to the newest eligible purchase.
    """
    eligible = eligible_incidents(incidents, opened_at)
    findings = [
        [c.issue for c in issue_candidates(
            incident,
            analyze_shipment(incident, opened_at),
            analyze_payment(incident, have_payment_evidence, opened_at),
        )]
        for incident in eligible
    ]
    for incident, found in zip(eligible, findings, strict=True):
        if claimed_issues and any(issue in claimed_issues for issue in found):
            return incident
    if not claimed_issues:
        for incident, found in zip(eligible, findings, strict=True):
            if any(issue not in BENIGN_ISSUES for issue in found):
                return incident
    return eligible[0] if eligible else None


def parse_policy(data: Any) -> PolicyParams | None:
    """Structured policy → PolicyParams; None when the shape is not recognised."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict):
        return None
    rules: dict[str, PolicyRule] = {}
    for issue, raw in data["rules"].items():
        if not isinstance(raw, dict) or "case_status" not in raw:
            continue
        refund = raw.get("refund_brl")
        rules[str(issue)] = PolicyRule(
            case_status=str(raw["case_status"]),
            recommended_action=raw.get("recommended_action"),
            refund_brl=None if refund is None else Decimal(str(refund)).quantize(CENT),
            responsible_parties=[p for p in raw.get("responsible_parties") or []
                                 if isinstance(p, dict)],
        )
    return PolicyParams(data.get("policy_version"), rules) if rules else None
