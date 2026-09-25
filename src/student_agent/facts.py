"""Normalize MCP evidence into typed facts and split one order's records into incidents.

The gateway can return several purchase episodes ("incidents") for the same order id
across history, items, payment, refund and shipment tools. Records are attached to the
latest incident whose purchase time is not after the record's own timestamp, and the
complaint is matched to the latest incident purchased no later than ``opened_at``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Field aliases: MCP data may use Olist column names or shorter summary names.
PURCHASED = ("order_purchase_timestamp", "purchased_at", "purchase_timestamp")
APPROVED = ("order_approved_at", "approved_at")
CARRIER = ("order_delivered_carrier_date", "delivered_carrier_at", "carrier_handoff_at")
DELIVERED = ("order_delivered_customer_date", "delivered_customer_at", "delivered_at")
ESTIMATED = ("order_estimated_delivery_date", "estimated_delivery_at", "estimated_at")
STATUS = ("order_status", "status")
LIMIT = ("shipping_limit_date", "shipping_limit_at")
EVENT_AT = ("event_at", "occurred_at", "timestamp", "created_at")
AMOUNT = ("amount_brl", "amount", "payment_value", "value")


def pick(record: Mapping[str, Any] | None, names: Iterable[str]) -> Any:
    if not isinstance(record, Mapping):
        return None
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return None


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def as_list(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Accept either a bare list of records or an object wrapping one under ``keys``."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


@dataclass(frozen=True)
class MoneyEvent:
    at: datetime
    kind: str
    amount: Decimal
    status: str
    source: str
    actor: str | None = None


@dataclass
class Incident:
    purchased_at: datetime
    order_row: dict[str, Any]
    row_source: str
    multiplicity: int = 1  # >1 when several purchases share one timestamp (indistinguishable)
    items: list[dict[str, Any]] = field(default_factory=list)
    payment_events: list[MoneyEvent] = field(default_factory=list)
    refund_events: list[MoneyEvent] = field(default_factory=list)
    shipment_events: list[MoneyEvent] = field(default_factory=list)

    @property
    def status(self) -> str | None:
        value = pick(self.order_row, STATUS)
        return str(value).lower() if value else None

    def when(self, names: Iterable[str]) -> datetime | None:
        return parse_dt(pick(self.order_row, names))

    @property
    def captures(self) -> list[MoneyEvent]:
        return [
            e for e in self.payment_events
            if e.kind in {"captured", "capture", "charged"} and e.status not in {"failed", "voided"}
        ]

    @property
    def item_total(self) -> Decimal | None:
        total = Decimal("0.00")
        for item in self.items:
            price, freight = money(item.get("price")), money(item.get("freight_value"))
            if price is None:
                return None
            total += price + (freight or Decimal("0.00"))
        return total if self.items else None

    @property
    def item_ids(self) -> list[str]:
        return list(dict.fromkeys(str(i["order_item_id"]) for i in self.items
                                  if i.get("order_item_id")))

    @property
    def seller_ids(self) -> list[str]:
        return list(dict.fromkeys(str(i["seller_id"]) for i in self.items if i.get("seller_id")))


def _money_events(rows: list[dict[str, Any]], source: str) -> list[MoneyEvent]:
    events = []
    for row in rows:
        at = parse_dt(pick(row, EVENT_AT))
        kind = str(row.get("event_type") or row.get("type") or "").lower()
        if at is None or not kind:
            continue
        events.append(MoneyEvent(
            at=at,
            kind=kind,
            amount=money(pick(row, AMOUNT)) or Decimal("0.00"),
            status=str(row.get("status") or "").lower(),
            source=source,
            actor=row.get("actor"),
        ))
    return events


def _attach(incidents: list[Incident], at: datetime | None) -> Incident | None:
    """Latest incident purchased at or before ``at``."""
    if at is None:
        return None
    eligible = [inc for inc in incidents if inc.purchased_at <= at]
    return max(eligible, key=lambda inc: inc.purchased_at) if eligible else None


def build_incidents(
    order_id: str,
    history_rows: list[dict[str, Any]],
    order_row: dict[str, Any] | None,
    items: list[dict[str, Any]],
    payment_data: Any,
    refund_data: Any,
    shipment_data: Any,
) -> list[Incident]:
    incidents: dict[datetime, Incident] = {}
    for source, rows in (("get_customer_history", history_rows), ("get_order", [order_row])):
        for row in rows:
            if not isinstance(row, dict) or str(row.get("order_id", order_id)) != order_id:
                continue
            purchased = parse_dt(pick(row, PURCHASED))
            if purchased is None:
                continue
            if purchased not in incidents:
                incidents[purchased] = Incident(purchased, row, source)
            elif source == "get_customer_history":
                incidents[purchased].multiplicity += 1
    ordered = sorted(incidents.values(), key=lambda inc: inc.purchased_at)
    if not ordered:
        return []

    for item in items:
        target = _attach(ordered, parse_dt(pick(item, LIMIT)))
        if target is not None:
            target.items.append(item)
    for event in _money_events(as_list(payment_data, "events"), "get_payment_timeline"):
        if (target := _attach(ordered, event.at)) is not None:
            target.payment_events.append(event)
    for event in _money_events(as_list(refund_data, "events", "refunds"), "get_refund_timeline"):
        if (target := _attach(ordered, event.at)) is not None:
            target.refund_events.append(event)
    for event in _money_events(as_list(shipment_data, "events"), "get_shipment_summary"):
        if (target := _attach(ordered, event.at)) is not None:
            target.shipment_events.append(event)
    return ordered


def eligible_incidents(incidents: list[Incident], opened_at: datetime | None) -> list[Incident]:
    """Purchases the complaint can refer to (made no later than it was opened), newest first."""
    eligible = [i for i in incidents if opened_at is None or i.purchased_at <= opened_at]
    return sorted(eligible, key=lambda i: i.purchased_at, reverse=True)


def select_incident(incidents: list[Incident], opened_at: datetime | None) -> Incident | None:
    """Default choice: the latest purchase made no later than the complaint was opened."""
    eligible = eligible_incidents(incidents, opened_at)
    return eligible[0] if eligible else None
