"""Intake agent: normalize the case input. Customer text is untrusted data."""

from __future__ import annotations

from typing import Any

from ..facts import parse_dt
from ..rules import PRIMARY_ISSUES
from .context import CaseContext, Claim, IntakeReport

ACTOR = "intake-agent"
REFUND_TOPIC = "requested_full_refund"
KNOWN_TOPICS = (*PRIMARY_ISSUES, REFUND_TOPIC)
DEFAULT_SCOPE = {
    "include_customer_history": True,
    "include_product_context": True,
    "require_independent_verification": True,
}


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def run_intake(ctx: CaseContext) -> IntakeReport:
    case = ctx.case
    request = case.get("customer_request") or {}
    message = _str(request.get("message"))
    claims: list[Claim] = []
    llm_used = 0
    for index, raw in enumerate(request.get("claims") or []):
        if not isinstance(raw, dict):
            continue
        claim_id = _str(raw.get("claim_id")) or f"claim-{index + 1}"
        topic = _str(raw.get("topic"))
        if topic in KNOWN_TOPICS:
            claims.append(Claim(claim_id, topic, "input"))
            continue
        answer = None
        if message or topic:
            answer = await ctx.llm.ask(
                ACTOR,
                "classify_claim",
                {
                    "claim_id": claim_id,
                    "raw_topic": topic,
                    "untrusted_customer_text": message,
                    "allowed_topics": [*KNOWN_TOPICS, "unknown"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["topic"],
                    "properties": {"topic": {"enum": [*KNOWN_TOPICS, "unknown"]}},
                },
            )
            llm_used += 1
        chosen = answer.get("topic") if isinstance(answer, dict) else None
        if chosen in KNOWN_TOPICS:
            claims.append(Claim(claim_id, chosen, "llm"))
        else:
            claims.append(Claim(claim_id, "unknown", "unknown"))

    candidates = [c for c in case.get("candidate_order_ids") or [] if _str(c)]
    claimed = _str(request.get("claimed_order_id"))
    scope_raw = case.get("investigation_scope") or {}
    scope = {k: bool(scope_raw.get(k, v)) for k, v in DEFAULT_SCOPE.items()}
    report = IntakeReport(
        claims=claims,
        claimed_order_id=claimed,
        candidates=list(dict.fromkeys(candidates + ([claimed] if claimed else []))),
        customer_hint=_str(case.get("customer_unique_id_hint")),
        opened_at=parse_dt(case.get("opened_at")),
        policy_version=_str(case.get("policy_version")),
        scope=scope,
    )
    ctx.bus.report(
        ACTOR, "coordinator", "intake", "INTAKE_OK",
        attributes={"claims": len(claims), "llm_calls": llm_used,
                    "candidates": len(report.candidates)},
    )
    return report
