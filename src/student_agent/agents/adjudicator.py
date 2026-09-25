"""Conflict resolver and adjudicator: rules first, LLM only when rules are not conclusive."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..facts import DELIVERED, ESTIMATED, PURCHASED, STATUS, Incident, parse_dt, pick
from ..rules import (
    BENIGN_ISSUES,
    PRIMARY_ISSUES,
    Candidate,
    PaymentAnalysis,
    ShipmentAnalysis,
    issue_candidates,
)
from .context import CaseContext, EntityReport, IntakeReport
from .specialists import Collected

CONFLICT_ACTOR = "conflict-resolver"
ACTOR = "adjudicator"
SELECTED_SOURCE = "get_customer_history"
WINDOW_CODE = "COMPLAINT_WINDOW_MATCH"
DOMAIN_TOOLS = {
    "order": ("get_order",),
    "customer": ("get_customer_history",),
    "item": ("get_order_items", "get_sellers"),
    "shipment": ("get_shipment_summary",),
    "payment": ("get_payment_timeline", "get_order_payments"),
    "refund": ("get_refund_timeline",),
    "policy": ("get_policy",),
    "product": ("get_product_context",),
}


@dataclass
class Decision:
    issue: str
    path: str  # rule | llm | degraded
    secondary: list[str] = field(default_factory=list)
    band: str = "high"
    override: bool = False
    candidates: list[Candidate] = field(default_factory=list)


def resolve_conflicts(
    ctx: CaseContext, entity: EntityReport, incident: Incident | None, collected: Collected,
    incidents: list[Incident],
) -> list[dict[str, Any]]:
    """Record where the order/shipment summary disagrees with the complaint incident."""
    conflicts: list[dict[str, Any]] = []
    if incident is None:
        if incidents:
            conflicts.append({"field": "complaint_incident",
                              "sources": ["get_order", "get_customer_history"],
                              "selected_source": None, "resolution_code": "UNRESOLVED"})
    else:
        order_row = entity.order_row
        if (incident.row_source != "get_order" and isinstance(order_row, dict)
                and parse_dt(pick(order_row, PURCHASED)) != incident.purchased_at):
            conflicts.append({"field": "order_record", "sources": ["get_order", SELECTED_SOURCE],
                              "selected_source": SELECTED_SOURCE, "resolution_code": WINDOW_CODE})
        summary = collected.shipment.data if collected.shipment else None
        if isinstance(summary, dict):
            differs = (
                parse_dt(pick(summary, DELIVERED)) != incident.when(DELIVERED)
                or parse_dt(pick(summary, ESTIMATED)) != incident.when(ESTIMATED)
                or str(pick(summary, STATUS) or "").lower() != (incident.status or "")
            )
            if differs:
                conflicts.append({"field": "shipment_timeline",
                                  "sources": ["get_shipment_summary", SELECTED_SOURCE],
                                  "selected_source": SELECTED_SOURCE,
                                  "resolution_code": WINDOW_CODE})
    ctx.bus.report(
        CONFLICT_ACTOR, ACTOR, "conflicts", "CONFLICTS_RESOLVED" if not any(
            c["selected_source"] is None for c in conflicts) else "CONFLICTS_UNRESOLVED",
        evidence_refs=ctx.store.refs_for("get_order", "get_customer_history",
                                         "get_shipment_summary"),
        attributes={"conflicts": len(conflicts)},
    )
    return conflicts


def has_domains(ctx: CaseContext, domains: tuple[str, ...]) -> bool:
    return all(ctx.store.refs_for(*DOMAIN_TOOLS[d]) for d in domains)


async def adjudicate(
    ctx: CaseContext,
    intake: IntakeReport,
    entity: EntityReport,
    incident: Incident | None,
    shipment: ShipmentAnalysis,
    payment: PaymentAnalysis,
    conflicts: list[dict[str, Any]],
) -> Decision:
    topics = intake.issue_topics
    if entity.status == "not_found" or incident is None:
        return _finish(ctx, Decision("insufficient_evidence", "rule"), conflicts)

    candidates = issue_candidates(incident, shipment, payment)
    actionable = [c for c in candidates if c.issue not in BENIGN_ISSUES]
    benign = [c for c in candidates if c.issue in BENIGN_ISSUES]
    core_present = has_domains(ctx, ("order", "payment")) and bool(
        ctx.store.refs_for("get_shipment_summary") or incident.status != "delivered")

    decision: Decision | None = None
    if len(actionable) == 1 and not benign:
        decision = Decision(actionable[0].issue, "rule")
    elif not actionable:
        if benign:
            decision = Decision(benign[0].issue, "rule")
        elif core_present:
            decision = Decision("unsupported_claim", "rule")
        else:
            decision = Decision("insufficient_evidence", "rule")
    else:
        # Several findings (benign ones only appear here for merged purchases): the claim
        # tells which one the complaint is about; evidence must still support it.
        matching = [c for c in candidates if c.issue in topics]
        if len(matching) == 1:
            decision = Decision(matching[0].issue, "rule",
                                secondary=[c.issue for c in candidates if c is not matching[0]])
        elif len(actionable) == 1:
            decision = Decision(actionable[0].issue, "rule",
                                secondary=[c.issue for c in benign])
        else:
            decision = await _llm_decide(ctx, intake, incident, shipment, payment, conflicts,
                                         candidates)
    decision.candidates = candidates
    chosen = next((c for c in candidates if c.issue == decision.issue), None)
    if chosen is not None and not has_domains(ctx, chosen.domains):
        decision = Decision("insufficient_evidence", decision.path, candidates=candidates)
    return _finish(ctx, decision, conflicts)


def _finish(ctx: CaseContext, decision: Decision, conflicts: list[dict[str, Any]]) -> Decision:
    ctx.bus.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor=ACTOR,
        decision_code=decision.issue.upper(),
        evidence_refs=ctx.store.refs_for("get_policy") or None,
        attributes={"path": decision.path, "conflicts": len(conflicts),
                    "override": decision.override, "model": ctx.llm.model
                    if decision.path == "llm" else None},
    )
    return decision


async def _llm_decide(
    ctx: CaseContext, intake: IntakeReport, incident: Incident, shipment: ShipmentAnalysis,
    payment: PaymentAnalysis, conflicts: list[dict[str, Any]], actionable: list[Candidate],
) -> Decision:
    request = ctx.case.get("customer_request") or {}
    payload = {
        "claim_topics": [c.topic for c in intake.claims],
        "untrusted_customer_text": request.get("message"),
        "incident": {
            "order_status": incident.status,
            "purchased_at": incident.purchased_at.isoformat(),
            "captures": [(e.at.isoformat(), str(e.amount)) for e in incident.captures],
            "refund_events": [(e.at.isoformat(), e.kind, e.status, str(e.amount))
                              for e in incident.refund_events],
            "shipment_events": [(e.at.isoformat(), e.kind, e.actor) for e in
                                incident.shipment_events],
        },
        "shipment_verdict": shipment.verdict,
        "payment_verdict": payment.verdict,
        "data_conflicts": conflicts,
        "rule_candidates": [{"issue": c.issue, "reason": c.reason} for c in actionable],
    }
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["primary_issue", "secondary_issues", "confidence_band"],
        "properties": {
            "primary_issue": {"enum": list(PRIMARY_ISSUES)},
            "secondary_issues": {"type": "array", "items": {"enum": list(PRIMARY_ISSUES)}},
            "confidence_band": {"enum": ["low", "medium", "high"]},
        },
    }
    answer = await ctx.llm.ask(ACTOR, "adjudicate", payload, schema)
    rule_issues = [c.issue for c in actionable]
    if isinstance(answer, dict) and answer.get("primary_issue") in PRIMARY_ISSUES:
        issue = answer["primary_issue"]
        override = issue not in rule_issues
        secondary = [i for i in answer.get("secondary_issues") or []
                     if i in PRIMARY_ISSUES and i != issue]
        band = answer.get("confidence_band", "medium")
        return Decision(issue, "llm", secondary, "medium" if override else band,
                        override=override)
    # Degraded: deterministic precedence (refund > payment > order > shipment).
    return Decision(rule_issues[0], "degraded", rule_issues[1:], "low")
