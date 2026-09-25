"""Coordinator for the L3B multi-agent workflow (see ARCHITECTURE.md)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .a2a import Bus
from .agents.adjudicator import DOMAIN_TOOLS, Decision, adjudicate, resolve_conflicts
from .agents.context import CaseContext, EntityReport, IntakeReport
from .agents.entity import resolve_entity
from .agents.intake import REFUND_TOPIC, run_intake
from .agents.specialists import Collected, analyze, collect, policy_params
from .agents.verifier import ISSUE_PAYMENT, verify
from .evidence import CaseEvidenceStore, Gateway
from .facts import Incident, build_incidents
from .llm import LLMClient
from .rules import PaymentAnalysis, PolicyParams, ShipmentAnalysis, choose_incident
from .trace import TraceWriter

COORDINATOR = "coordinator"
_LLM: LLMClient | None = None

DEFAULT_STATUS = {"insufficient_evidence": "needs_investigation",
                  "unsupported_claim": "no_action", "valid_split_payment": "no_action",
                  "refund_pending": "needs_investigation"}
DEFAULT_PARTY = {
    "late_delivery_seller": "seller", "unavailable_order_paid": "seller",
    "late_delivery_logistics": "logistics_provider", "canceled_order_paid": "platform",
    "duplicate_charge": "payment_provider", "payment_mismatch": "payment_provider",
    "refund_pending": "payment_provider", "refund_failed": "payment_provider",
    "unsupported_claim": "customer", "valid_split_payment": "customer",
}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim",
                   "canceled_order_paid", "unavailable_order_paid", "insufficient_evidence"}
BAND_CONFIDENCE = {"high": 0.75, "medium": 0.65, "low": 0.5}


def _llm() -> LLMClient:
    global _LLM
    if _LLM is None:
        _LLM = LLMClient(Path.cwd())
    return _LLM


async def solve_case(
    case: dict[str, Any], gateway: Gateway, trace: TraceWriter, llm: LLMClient | None = None
) -> dict[str, Any]:
    case_id = case["case_id"]
    bus = Bus(case_id, trace)
    ctx = CaseContext(case, CaseEvidenceStore(case_id, gateway, trace), bus, llm or _llm())

    bus.assign(COORDINATOR, "intake-agent", "intake")
    intake = await run_intake(ctx)

    bus.assign(COORDINATOR, "entity-agent", "entity_resolution")
    entity = await resolve_entity(ctx, intake)

    collected = await collect(ctx, intake, entity)
    policy = await policy_params(ctx, collected)

    incidents: list[Incident] = []
    incident = None
    if entity.order_id:
        incidents = build_incidents(
            entity.order_id,
            entity.history_rows,
            entity.order_row,
            collected.item_rows,
            collected.payment.data if collected.payment else None,
            collected.refund.data if collected.refund else None,
            collected.shipment.data if collected.shipment else None,
        )
        incident = choose_incident(incidents, intake.opened_at, intake.issue_topics,
                                   collected.payment is not None)
    shipment, payment = analyze(ctx, incident, collected, intake.opened_at)

    bus.assign(COORDINATOR, "conflict-resolver", "conflicts")
    conflicts = resolve_conflicts(ctx, entity, incident, collected, incidents)
    bus.assign(COORDINATOR, "adjudicator", "adjudicate")
    decision = await adjudicate(ctx, intake, entity, incident, shipment, payment, conflicts)

    output = build_output(ctx, intake, entity, incident, collected, shipment, payment,
                          policy, conflicts, decision)
    bus.report("adjudicator", "verifier", "verify", "DRAFT_READY",
               evidence_refs=output["evidence_refs"])
    unresolved = sum(1 for c in conflicts if c["selected_source"] is None)
    return verify(ctx, output, intake.candidates, decision.path, unresolved)


def _payment_verdict(decision: Decision, payment: PaymentAnalysis) -> str:
    """Align the payment verdict with the adjudicated issue when evidence shows that state.

    One purchase can carry several payment signals (e.g. mismatch and a pending refund);
    the verdict must describe the one the case is decided on, not a fixed precedence.
    """
    evidenced = {c.issue for c in decision.candidates}
    if decision.issue in ISSUE_PAYMENT and decision.issue in evidenced:
        return ISSUE_PAYMENT[decision.issue]
    return payment.verdict


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _refund_amount(issue: str, rule_refund: Decimal | None, incident: Incident | None,
                   payment: PaymentAnalysis) -> Decimal:
    refundable = payment.refundable or Decimal("0.00")
    if rule_refund is not None:
        return min(rule_refund, refundable)
    # No policy amount: fall back to what the evidence itself supports.
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return refundable
    if issue == "duplicate_charge" and payment.duplicate_amount is not None:
        return min(payment.duplicate_amount, refundable)
    if issue == "refund_failed" and incident is not None:
        failed = [e.amount for e in incident.refund_events if e.status == "failed"]
        return min(sum(failed, Decimal("0.00")), refundable)
    return Decimal("0.00")


def _parties(issue: str, policy: PolicyParams, incident: Incident | None,
             shipment: ShipmentAnalysis) -> list[dict[str, Any]]:
    rule = policy.rules.get(issue)
    raw = rule.responsible_parties if rule else (
        [{"party_type": DEFAULT_PARTY[issue], "party_id": None}] if issue in DEFAULT_PARTY
        else [{"party_type": "unknown", "party_id": None}])
    known_sellers = shipment.late_seller_ids or (incident.seller_ids if incident else [])
    parties: list[dict[str, Any]] = []
    for party in raw:
        party_type = party.get("party_type", "unknown")
        if party_type == "seller":
            # Policy seller ids are templates; responsibility goes to sellers in evidence.
            pid = party.get("party_id")
            ids = [pid] if pid in known_sellers else known_sellers or [None]
            parties += [{"party_type": "seller", "party_id": s} for s in ids]
        else:
            parties.append({"party_type": party_type, "party_id": party.get("party_id")})
    unique = {(p["party_type"], p["party_id"]): p for p in parties}
    return list(unique.values())[:5]


def _confidence(decision: Decision, intake: IntakeReport, entity: EntityReport,
                conflicts: list[dict[str, Any]]) -> float:
    topics = intake.issue_topics
    if decision.issue == "insufficient_evidence":
        base = 0.3
    elif decision.path == "rule":
        base = 0.9 if decision.issue in topics or not topics else 0.72
        if decision.issue == "unsupported_claim":
            base = 0.8
        if decision.secondary:
            base -= 0.08
    elif decision.path == "llm":
        base = BAND_CONFIDENCE.get(decision.band, 0.6) - (0.1 if decision.override else 0)
    else:
        base = 0.45
    base -= 0.1 * sum(1 for c in conflicts if c["selected_source"] is None)
    return round(max(0.05, min(base, entity.confidence, 0.97)), 2)


def _evidence_refs(ctx: CaseContext, intake: IntakeReport, issue: str, incident: Incident | None,
                   conflicts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Refs per domain that the final answer actually relies on."""
    wanted = ["order", "customer", "item", "payment", "policy"]
    conflict_tools = {s for c in conflicts for s in c["sources"]}
    if issue in SHIPMENT_ISSUES or "get_shipment_summary" in conflict_tools:
        wanted.append("shipment")
    if issue.startswith("refund_") or (incident is not None and incident.refund_events):
        wanted.append("refund")
    if intake.scope["include_product_context"]:
        wanted.append("product")
    return {d: ctx.store.refs_for(*DOMAIN_TOOLS[d]) for d in wanted}


def _claims(intake: IntakeReport, decision: Decision, refund: Decimal,
            payment: PaymentAnalysis, confidence: float,
            refs: dict[str, list[str]]) -> list[dict[str, Any]]:
    issue = decision.issue
    all_refs = [r for rs in refs.values() for r in rs]
    out = []
    for claim in intake.claims[:5]:
        if claim.topic == "unknown" or issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif claim.topic == REFUND_TOPIC:
            refundable = payment.refundable or Decimal("0.00")
            if refund <= 0:
                verdict = "unsupported"
            elif refund >= refundable - Decimal("0.01"):
                verdict = "supported"
            else:
                verdict = "partially_supported"
        elif issue == "unsupported_claim":
            verdict = "unsupported"
        elif claim.topic == issue:
            verdict = "supported"
        elif claim.topic in decision.secondary:
            verdict = "partially_supported"
        else:
            verdict = "unsupported"
        linked = refs.get("payment", []) + refs.get("policy", []) if claim.topic == REFUND_TOPIC \
            else all_refs
        out.append({"claim_id": claim.claim_id, "verdict": verdict, "confidence": confidence,
                    "evidence_refs": list(dict.fromkeys(linked))[:30]})
    return out


def build_output(
    ctx: CaseContext, intake: IntakeReport, entity: EntityReport, incident: Incident | None,
    collected: Collected, shipment: ShipmentAnalysis, payment: PaymentAnalysis,
    policy: PolicyParams, conflicts: list[dict[str, Any]], decision: Decision,
) -> dict[str, Any]:
    issue = decision.issue
    rule = policy.rules.get(issue)
    status = rule.case_status if rule else DEFAULT_STATUS.get(issue, "action_required")
    if issue == "insufficient_evidence":
        status = "needs_investigation"
    refund = (_refund_amount(issue, rule.refund_brl if rule else None, incident, payment)
              if status == "action_required" else Decimal("0.00"))
    order_id = entity.order_id
    actions = [rule.recommended_action] if rule and rule.recommended_action else []
    if not actions:
        actions = ["escalate_for_investigation" if status == "needs_investigation"
                   else "document_no_action" if status == "no_action" else "review_case"]
    confidence = _confidence(decision, intake, entity, conflicts)
    refs = _evidence_refs(ctx, intake, issue, incident, conflicts)
    evidence_refs = list(dict.fromkeys(r for rs in refs.values() for r in rs))[:30]

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": list(dict.fromkeys(decision.secondary))[:10],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id and entity.status != "not_found" else [],
            "item_ids": incident.item_ids if incident else [],
            "seller_ids": incident.seller_ids if incident else [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": _claims(intake, decision, refund, payment, confidence, refs),
        "entity_resolution": {
            "status": entity.status,
            "resolved_order_ids": [order_id] if order_id and entity.status != "not_found"
            else [],
            "rejected_candidates": list(entity.rejected)[:20],
            "confidence": entity.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": entity.related_order_ids[:20],
        },
        "shipment_analysis": {
            "verdict": shipment.verdict,
            "late_seller_ids": shipment.late_seller_ids[:20],
            "timeline_complete": shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": _payment_verdict(decision, payment),
            "captured_total_brl": _f(payment.captured),
            "refunded_total_brl": _f(payment.refunded),
            "refundable_total_brl": _f(payment.refundable),
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": _parties(issue, policy, incident, shipment),
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": [{"reason_code": actions[0], "amount_brl": float(refund),
                              "entity_id": order_id}] if refund > 0 else [],
        },
        "resolution_actions": actions,
    }
