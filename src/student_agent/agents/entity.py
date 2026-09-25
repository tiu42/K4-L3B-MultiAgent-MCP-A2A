"""Entity/customer agent: resolve the complained-about order from candidates using evidence.

Candidates are never rejected by ID format or list position, only by evidence:
missing from the customer's authoritative history, or not found by the order tool.
"""

from __future__ import annotations

from typing import Any

from ..facts import as_list
from .context import CaseContext, EntityReport, IntakeReport

ACTOR = "entity-agent"
MAX_ORDER_LOOKUPS = 3
RESOLVE_THRESHOLD = 3


async def resolve_entity(ctx: CaseContext, intake: IntakeReport) -> EntityReport:
    store = ctx.store
    scores: dict[str, int] = {c: 0 for c in intake.candidates}
    rejected: dict[str, str] = {}
    history_rows: list[dict[str, Any]] = []
    customer_id: str | None = None

    # History is fetched when the scope asks for it, or when several candidates need it
    # to be told apart; a single candidate is checked by get_order alone.
    wants_history = intake.scope["include_customer_history"] or len(intake.candidates) > 1
    if intake.customer_hint and wants_history:
        history = await store.fetch(ACTOR, "get_customer_history",
                                    customer_unique_id=intake.customer_hint)
        if history is not None:
            history_rows = as_list(history.data, "orders")
            if isinstance(history.data, dict):
                customer_id = history.data.get("customer_unique_id") or intake.customer_hint
    history_ids = {str(r.get("order_id")) for r in history_rows if r.get("order_id")}
    if history_ids:
        for candidate in scores:
            if candidate in history_ids:
                scores[candidate] += 2
            else:
                rejected[candidate] = "NOT_IN_CUSTOMER_HISTORY"
        if scores and len(rejected) == len(scores):
            rejected.clear()  # the hint itself may be wrong: fall back to order lookups
    if intake.claimed_order_id in scores:
        scores[intake.claimed_order_id] += 1  # weak tie-break signal only

    ranked = sorted((c for c in scores if c not in rejected), key=lambda c: -scores[c])
    order_rows: dict[str, dict[str, Any]] = {}
    for candidate in ranked[:MAX_ORDER_LOOKUPS]:
        order = await store.fetch(ACTOR, "get_order", order_id=candidate)
        if order is None or not isinstance(order.data, dict):
            rejected[candidate] = "ORDER_NOT_FOUND"
            continue
        order_rows[candidate] = order.data
        scores[candidate] += 2
        history_customer = {r.get("customer_id") for r in history_rows
                            if str(r.get("order_id")) == candidate}
        if order.data.get("customer_id") in history_customer:
            scores[candidate] += 1
        others_pending = [c for c in ranked if c not in order_rows and c not in rejected
                          and c != candidate]
        if scores[candidate] >= RESOLVE_THRESHOLD and not others_pending:
            break

    qualified = [c for c in order_rows if scores[c] >= RESOLVE_THRESHOLD]
    qualified.sort(key=lambda c: -scores[c])
    if len(qualified) == 1 or (len(qualified) > 1 and scores[qualified[0]] > scores[qualified[1]]):
        status, order_id = "resolved", qualified[0]
        confidence = 0.95 if history_ids and qualified[0] in history_ids else 0.8
    elif qualified:
        status, order_id, confidence = "ambiguous", qualified[0], 0.45
    else:
        status, order_id, confidence = "not_found", None, 0.3

    related = sorted({str(r["order_id"]) for r in history_rows
                      if r.get("order_id") and str(r["order_id"]) != order_id})
    report = EntityReport(
        status=status,
        order_id=order_id,
        rejected={c: r for c, r in rejected.items() if c != order_id},
        confidence=confidence,
        customer_unique_id=customer_id,
        related_order_ids=related,
        history_rows=history_rows,
        order_row=order_rows.get(order_id) if order_id else None,
    )
    ctx.bus.report(
        ACTOR, "coordinator", "entity_resolution", f"ENTITY_{status.upper()}",
        evidence_refs=store.refs_for("get_customer_history", "get_order"),
        attributes={"rejected": len(report.rejected), "lookups": len(order_rows)},
    )
    return report
