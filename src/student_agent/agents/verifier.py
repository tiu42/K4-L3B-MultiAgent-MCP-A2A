"""Verifier: pure-code invariant checks on the draft output (no MCP, no LLM)."""

from __future__ import annotations

from typing import Any

from .context import CaseContext

ACTOR = "verifier"
REFUND_ACTION_WORDS = ("refund",)
ISSUE_SHIPMENT = {"late_delivery_seller": "seller_delay",
                  "late_delivery_logistics": "logistics_delay"}
ISSUE_PAYMENT = {"duplicate_charge": "duplicate_capture", "payment_mismatch": "capture_mismatch",
                 "refund_pending": "refund_pending", "refund_failed": "refund_failed"}
ISSUE_PARTY = {"late_delivery_seller": "seller", "late_delivery_logistics": "logistics_provider"}


def _round(value: float) -> float:
    return round(float(value) + 0.0, 2)


def verify(ctx: CaseContext, output: dict[str, Any], candidates: list[str],
           path: str, unresolved_conflicts: int) -> dict[str, Any]:
    repaired: list[str] = []
    downgraded: list[str] = []
    a = output["assessment"]
    issue = a["primary_issue"]

    # 3. Evidence ownership: only refs MCP returned for this case, deduplicated.
    owned = [r for r in dict.fromkeys(output["evidence_refs"]) if ctx.store.owns(r)][:30]
    if owned != output["evidence_refs"]:
        output["evidence_refs"] = owned
        repaired.append("EVIDENCE_SCOPE")
    for claim in output.get("claim_assessments", []):
        linked = [r for r in claim["evidence_refs"] if r in owned]
        if linked != claim["evidence_refs"]:
            claim["evidence_refs"] = linked
            repaired.append("CLAIM_LINKAGE")

    # 2. Entity scope.
    er = output["entity_resolution"]
    if output["affected_entities"]["order_ids"] != er["resolved_order_ids"]:
        output["affected_entities"]["order_ids"] = list(er["resolved_order_ids"])
        repaired.append("ENTITY_SCOPE")
    clean_rejected = [c for c in er["rejected_candidates"]
                      if c in candidates and c not in er["resolved_order_ids"]]
    if clean_rejected != er["rejected_candidates"]:
        er["rejected_candidates"] = clean_rejected
        repaired.append("REJECTED_SCOPE")

    # 5. Timeline.
    sa = output["shipment_analysis"]
    if sa["verdict"] == "seller_delay":
        sellers = output["affected_entities"]["seller_ids"]
        for seller in sa["late_seller_ids"]:
            if seller not in sellers:
                sellers.append(seller)
                repaired.append("LATE_SELLER_ENTITY")
        if not sa["late_seller_ids"]:
            downgraded.append("SELLER_DELAY_WITHOUT_SELLER")
    if not sa["timeline_complete"] and sa["verdict"] == "on_time":
        sa["verdict"] = "insufficient_evidence"
        repaired.append("INCOMPLETE_TIMELINE")

    # 6 + 8. Money and status/refund/action consistency.
    fr = output["financial_resolution"]
    has_refund = bool(fr["recommended_refund_brl"] or fr["refund_lines"])
    if a["case_status"] != "action_required" and has_refund:
        fr["recommended_refund_brl"], fr["refund_lines"] = 0.0, []
        repaired.append("NO_ACTION_REFUND")
    if a["case_status"] != "action_required":
        kept = [x for x in output["resolution_actions"]
                if not any(w in x for w in REFUND_ACTION_WORDS) or x.startswith("monitor")]
        if kept != output["resolution_actions"]:
            output["resolution_actions"] = kept
            repaired.append("NO_ACTION_ACTIONS")
    refundable = output["payment_analysis"]["refundable_total_brl"]
    for line in fr["refund_lines"]:
        line["amount_brl"] = _round(line["amount_brl"])
    total = _round(sum(line["amount_brl"] for line in fr["refund_lines"]))
    if refundable is not None and total > refundable + 0.005:
        scale_to = _round(refundable)
        fr["refund_lines"] = ([{**fr["refund_lines"][0], "amount_brl": scale_to}]
                              if scale_to > 0 else [])
        total = scale_to
        repaired.append("REFUND_CAP")
    if fr["recommended_refund_brl"] != total:
        fr["recommended_refund_brl"] = total
        repaired.append("REFUND_TOTAL")
    if a["case_status"] == "action_required" and not output["resolution_actions"]:
        downgraded.append("NO_ACTION_FOR_REQUIRED_CASE")
    if issue == "insufficient_evidence" and a["case_status"] != "needs_investigation":
        a["case_status"] = "needs_investigation"
        repaired.append("INSUFFICIENT_STATUS")
    if issue in ISSUE_SHIPMENT and sa["verdict"] != ISSUE_SHIPMENT[issue]:
        downgraded.append("SHIPMENT_VERDICT_MISMATCH")
    if issue in ISSUE_PAYMENT and output["payment_analysis"]["verdict"] != ISSUE_PAYMENT[issue]:
        downgraded.append("PAYMENT_VERDICT_MISMATCH")
    parties = {p["party_type"] for p in output["root_cause_analysis"]["responsible_parties"]}
    if issue in ISSUE_PARTY and ISSUE_PARTY[issue] not in parties:
        downgraded.append("RESPONSIBLE_PARTY_MISMATCH")
    output["resolution_actions"] = list(dict.fromkeys(output["resolution_actions"]))

    # 7. Conflicts.
    for conflict in output["data_conflicts"]:
        if conflict["selected_source"] not in (*conflict["sources"], None):
            conflict["selected_source"] = None
            conflict["resolution_code"] = "UNRESOLVED"
            repaired.append("CONFLICT_SOURCE")

    # 9. Confidence bounds.
    cap = er["confidence"]
    if unresolved_conflicts:
        cap = min(cap, 0.6)
    if issue == "insufficient_evidence":
        cap = min(cap, 0.4)
    if path == "degraded":
        cap = min(cap, 0.5)
    if downgraded:
        cap = min(cap, 0.55)
    new_conf = round(max(0.05, min(a["confidence"], cap, 0.97)), 2)
    a["confidence"] = new_conf
    for claim in output.get("claim_assessments", []):
        claim["confidence"] = round(max(0.05, min(claim["confidence"], cap, 0.97)), 2)

    # 1. Full public schema.
    ctx.bus.trace.contracts.validate_output(output, f"outputs/{ctx.case_id}.json")

    code = "DOWNGRADED" if downgraded else "REPAIRED" if repaired else "PASS"
    ctx.bus.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor=ACTOR,
        target="coordinator",
        decision_code=code,
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"repairs": len(repaired), "downgrades": len(downgraded),
                    "first_issue": (downgraded or repaired or [None])[0]},
    )
    return output
