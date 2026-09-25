"""Specialist agents: order/product, shipment, payment/refund and policy.

Phase 1 (``collect``): each specialist fetches only its own tools, in parallel. The shipment
summary is fetched afterwards (``collect_shipment``) and only when it can change the decision.
Phase 2 (``analyze``): once the complaint incident is known, each specialist analyses its
domain for that incident and hands its report back to the coordinator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..evidence import Evidence
from ..facts import Incident, as_list
from ..rules import (
    BENIGN_ISSUES,
    PRIMARY_ISSUES,
    PaymentAnalysis,
    PolicyParams,
    ShipmentAnalysis,
    analyze_payment,
    analyze_shipment,
    issue_candidates,
    parse_policy,
)
from .context import CaseContext, EntityReport, IntakeReport

ORDER, SHIPMENT, PAYMENT, POLICY = "order-agent", "shipment-agent", "payment-agent", "policy-agent"
_POLICY_CACHE: dict[str, PolicyParams] = {}  # keyed by result_hash: content, never refs
# Claimed issues that are decided on delivery evidence (or need it to be ruled out).
SHIPMENT_TOPICS = frozenset({"late_delivery_seller", "late_delivery_logistics",
                             "canceled_order_paid", "unavailable_order_paid", "unsupported_claim"})


@dataclass
class Collected:
    items: Evidence | None = None
    sellers: Evidence | None = None
    product: Evidence | None = None
    payment: Evidence | None = None
    refund: Evidence | None = None
    shipment: Evidence | None = None
    policy: Evidence | None = None

    @property
    def item_rows(self) -> list[dict[str, Any]]:
        return as_list(self.items.data, "items") if self.items else []


async def _order_agent(ctx: CaseContext, intake: IntakeReport, order_id: str, out: Collected):
    out.items = await ctx.store.fetch(ORDER, "get_order_items", order_id=order_id)
    if out.items is not None and any(not r.get("seller_id") for r in out.item_rows):
        out.sellers = await ctx.store.fetch(ORDER, "get_sellers", order_id=order_id)
    if intake.scope["include_product_context"]:
        out.product = await ctx.store.fetch(ORDER, "get_product_context", order_id=order_id)


async def _payment_agent(ctx: CaseContext, order_id: str, out: Collected):
    out.payment = await ctx.store.fetch(PAYMENT, "get_payment_timeline", order_id=order_id)
    # A tool error here usually means "no refund records"; it is not retried.
    out.refund = await ctx.store.fetch(PAYMENT, "get_refund_timeline", order_id=order_id)


async def _policy_agent(ctx: CaseContext, intake: IntakeReport, out: Collected):
    if intake.policy_version:
        out.policy = await ctx.store.fetch(POLICY, "get_policy",
                                           policy_version=intake.policy_version)


async def collect(ctx: CaseContext, intake: IntakeReport, entity: EntityReport) -> Collected:
    out = Collected()
    order_id = entity.order_id
    bus = ctx.bus
    jobs = [(_policy_agent(ctx, intake, out), POLICY)]
    if order_id:
        jobs += [
            (_order_agent(ctx, intake, order_id, out), ORDER),
            (_payment_agent(ctx, order_id, out), PAYMENT),
        ]
    for _, agent in jobs:
        bus.assign("coordinator", agent, "investigate")
    await asyncio.gather(*(job for job, _ in jobs))
    return out


def shipment_needed(intake: IntakeReport, incidents: list[Incident], incident: Incident | None,
                    collected: Collected) -> bool:
    """Whether the shipment summary can still change the output for this incident.

    It cannot when the order id covers a single purchase (so the per-order summary cannot
    contradict the complaint's incident), the customer raises no delivery issue, the order
    record already shows a complete, on-time delivery, and payment/refund evidence alone
    confirms an actionable issue the customer claimed. Otherwise the summary is fetched.
    """
    if incident is None or len(incidents) != 1 or incident.status != "delivered":
        return True
    if any(topic in SHIPMENT_TOPICS for topic in intake.issue_topics):
        return True
    without_events = analyze_shipment(incident, intake.opened_at)
    if without_events.verdict != "on_time" or not without_events.timeline_complete:
        return True
    payment = analyze_payment(incident, collected.payment is not None, intake.opened_at)
    confirmed = {c.issue for c in issue_candidates(incident, without_events, payment)
                 if c.issue not in BENIGN_ISSUES and "shipment" not in c.domains}
    return not confirmed.intersection(intake.issue_topics)


async def collect_shipment(ctx: CaseContext, order_id: str, out: Collected) -> None:
    ctx.bus.assign("coordinator", SHIPMENT, "investigate")
    out.shipment = await ctx.store.fetch(SHIPMENT, "get_shipment_summary", order_id=order_id)


async def policy_params(ctx: CaseContext, collected: Collected) -> PolicyParams:
    evidence = collected.policy
    if evidence is None:
        params = PolicyParams(None, {}, "missing")
    elif evidence.result_hash in _POLICY_CACHE:
        params = _POLICY_CACHE[evidence.result_hash]
    else:
        params = parse_policy(evidence.data)
        if params is None:
            params = await _policy_from_llm(ctx, evidence)
        _POLICY_CACHE[evidence.result_hash] = params
    ctx.bus.report(
        POLICY, "coordinator", "investigate", f"POLICY_{params.source.upper()}",
        evidence_refs=[evidence.ref] if evidence else [],
        attributes={"rules": len(params.rules)},
    )
    return params


async def _policy_from_llm(ctx: CaseContext, evidence: Evidence) -> PolicyParams:
    rule_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["issue", "case_status", "recommended_action", "refund_brl", "party_types"],
        "properties": {
            "issue": {"enum": list(PRIMARY_ISSUES)},
            "case_status": {"enum": ["action_required", "no_action", "needs_investigation"]},
            "recommended_action": {"type": "string"},
            "refund_brl": {"type": ["number", "null"]},
            "party_types": {"type": "array", "items": {"enum": [
                "seller", "platform", "logistics_provider", "payment_provider", "customer",
                "unknown"]}},
        },
    }
    answer = await ctx.llm.ask(
        POLICY, "parse_policy", {"policy_document": evidence.data},
        {"type": "object", "additionalProperties": False, "required": ["rules"],
         "properties": {"rules": {"type": "array", "items": rule_schema}}},
    )
    if not isinstance(answer, dict):
        return PolicyParams(None, {}, "missing")
    structured = {"rules": {
        r["issue"]: {
            "case_status": r["case_status"],
            "recommended_action": r["recommended_action"],
            "refund_brl": r["refund_brl"],
            "responsible_parties": [{"party_type": t, "party_id": None}
                                    for t in r["party_types"]],
        } for r in answer.get("rules", []) if isinstance(r, dict)
    }}
    params = parse_policy(structured) or PolicyParams(None, {}, "missing")
    params.source = "llm" if params.rules else "missing"
    return params


def analyze(
    ctx: CaseContext, incident: Incident | None, collected: Collected,
    as_of: datetime | None = None,
) -> tuple[ShipmentAnalysis, PaymentAnalysis]:
    bus, store = ctx.bus, ctx.store
    if incident is None:
        shipment = ShipmentAnalysis("insufficient_evidence", [], False)
        payment = PaymentAnalysis("insufficient_evidence", None, None, None)
    else:
        shipment = analyze_shipment(incident, as_of)
        payment = analyze_payment(incident, collected.payment is not None, as_of)
    bus.report(ORDER, "coordinator", "investigate", "ORDER_FACTS",
               evidence_refs=store.refs_for("get_order_items", "get_sellers",
                                            "get_product_context"),
               attributes={"items": len(incident.items) if incident else 0})
    if collected.shipment is not None:
        bus.report(SHIPMENT, "coordinator", "investigate", shipment.verdict.upper(),
                   evidence_refs=store.refs_for("get_shipment_summary"),
                   attributes={"timeline_complete": shipment.timeline_complete})
    bus.report(PAYMENT, "coordinator", "investigate", payment.verdict.upper(),
               evidence_refs=store.refs_for("get_payment_timeline", "get_refund_timeline"),
               attributes={"captured": float(payment.captured or 0)})
    return shipment, payment
