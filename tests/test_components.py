"""Unit tests on synthetic data (no competition payload, no network)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from student_agent.a2a import Bus, ProtocolError
from student_agent.contracts import Contracts
from student_agent.evidence import CaseEvidenceStore, ToolPermissionError
from student_agent.facts import build_incidents, parse_dt, select_incident
from student_agent.rules import analyze_payment, analyze_shipment, issue_candidates
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
ORDER = "a" * 32


def _trace(tmp_path: Path) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))


def _row(purchased: str, status: str, carrier: str | None, delivered: str | None,
         estimated: str) -> dict:
    return {"order_id": ORDER, "customer_id": "c1", "order_status": status,
            "order_purchase_timestamp": purchased, "order_delivered_carrier_date": carrier,
            "order_delivered_customer_date": delivered,
            "order_estimated_delivery_date": estimated}


def _item(limit: str, seller: str = "s1", price: str = "79.00", freight: str = "10.00") -> dict:
    return {"order_id": ORDER, "order_item_id": "i1", "seller_id": seller,
            "shipping_limit_date": limit, "price": price, "freight_value": freight}


def _capture(at: str, amount: str) -> dict:
    return {"event_at": at, "event_type": "captured", "amount_brl": amount, "status": "confirmed"}


def _incident(history, items, payments, refunds=(), opened="2018-06-01T00:00:00-03:00"):
    incidents = build_incidents(ORDER, history, None, items, {"events": payments},
                                {"events": list(refunds)}, {"events": []})
    return select_incident(incidents, parse_dt(opened))


class FakeGateway:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("transient")
        return {"schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_{'x' * 20}{self.calls}", "result_hash": "sha256:" + "0" * 64,
                "domain": "order", "data": {"order_id": arguments.get("order_id")}}


def test_incident_selection_uses_latest_purchase_before_complaint() -> None:
    history = [
        _row("2018-05-01T09:00:00-03:00", "delivered", "2018-05-02T09:00:00-03:00",
             "2018-05-05T09:00:00-03:00", "2018-05-10T09:00:00-03:00"),
        _row("2018-07-01T09:00:00-03:00", "canceled", None, None, "2018-07-10T09:00:00-03:00"),
    ]
    incident = _incident(history, [], [_capture("2018-05-01T10:00:00-03:00", "89.00"),
                                       _capture("2018-07-01T10:00:00-03:00", "79.00")])
    assert incident is not None and incident.status == "delivered"
    assert [str(c.amount) for c in incident.captures] == ["89.00"]


def test_split_vs_duplicate_capture() -> None:
    history = [_row("2018-05-01T09:00:00-03:00", "delivered", "2018-05-02T09:00:00-03:00",
                    "2018-05-05T09:00:00-03:00", "2018-05-10T09:00:00-03:00")]
    items = [_item("2018-05-03T09:00:00-03:00")]
    split = _incident(history, items, [_capture("2018-05-01T10:00:00-03:00", "44.50"),
                                       _capture("2018-05-01T11:00:00-03:00", "44.50")])
    dup = _incident(history, items, [_capture("2018-05-01T10:00:00-03:00", "64.00"),
                                     _capture("2018-05-01T11:00:00-03:00", "64.00")])
    assert analyze_payment(split, True).split
    dup_payment = analyze_payment(dup, True)
    assert dup_payment.verdict == "duplicate_capture"
    assert str(dup_payment.duplicate_amount) == "64.00"


def test_late_delivery_attributed_to_seller_or_logistics() -> None:
    late_handoff = [_row("2018-05-01T09:00:00-03:00", "delivered", "2018-05-06T09:00:00-03:00",
                         "2018-05-12T09:00:00-03:00", "2018-05-10T09:00:00-03:00")]
    on_time_handoff = [_row("2018-05-01T09:00:00-03:00", "delivered", "2018-05-02T09:00:00-03:00",
                            "2018-05-12T09:00:00-03:00", "2018-05-10T09:00:00-03:00")]
    items = [_item("2018-05-03T09:00:00-03:00", seller="s9")]
    seller = analyze_shipment(_incident(late_handoff, items, []))
    logistics = analyze_shipment(_incident(on_time_handoff, items, []))
    assert (seller.verdict, seller.late_seller_ids) == ("seller_delay", ["s9"])
    assert logistics.verdict == "logistics_delay"


def test_refund_state_and_candidates() -> None:
    history = [_row("2018-05-01T09:00:00-03:00", "delivered", "2018-05-02T09:00:00-03:00",
                    "2018-05-05T09:00:00-03:00", "2018-05-10T09:00:00-03:00")]
    refund = {"event_at": "2018-05-20T09:00:00-03:00", "event_type": "refund_requested",
              "amount_brl": "52.00", "status": "failed"}
    incident = _incident(history, [_item("2018-05-03T09:00:00-03:00")],
                         [_capture("2018-05-01T10:00:00-03:00", "52.00")], [refund])
    payment = analyze_payment(incident, True)
    shipment = analyze_shipment(incident)
    assert [c.issue for c in issue_candidates(incident, shipment, payment)] == ["refund_failed"]


def test_evidence_store_permission_cache_and_retry(tmp_path: Path) -> None:
    gateway = FakeGateway(fail_times=1)
    store = CaseEvidenceStore("CASE_001", gateway, _trace(tmp_path))
    store_run = asyncio.run
    first = store_run(store.fetch("entity-agent", "get_order", order_id="o1"))
    again = store_run(store.fetch("entity-agent", "get_order", order_id="o1"))
    assert first is not None and again is first
    assert gateway.calls == 2  # one transient failure retried, then served from cache
    with pytest.raises(ToolPermissionError):
        store_run(store.fetch("shipment-agent", "get_order", order_id="o1"))


def test_evidence_store_budget(tmp_path: Path) -> None:
    store = CaseEvidenceStore("CASE_001", FakeGateway(), _trace(tmp_path), budget=1)
    assert asyncio.run(store.fetch("entity-agent", "get_order", order_id="o1")) is not None
    assert asyncio.run(store.fetch("entity-agent", "get_order", order_id="o2")) is None
    assert store.failures[-1].code == "BUDGET_EXHAUSTED"


def test_bus_rejects_duplicate_assignment(tmp_path: Path) -> None:
    bus = Bus("CASE_001", _trace(tmp_path))
    bus.assign("coordinator", "entity-agent", "entity_resolution")
    with pytest.raises(ProtocolError):
        bus.assign("coordinator", "entity-agent", "entity_resolution")


def test_parse_dt_requires_timezone() -> None:
    assert parse_dt("2018-01-01T09:00:00") is None
    assert parse_dt("2018-01-01T09:00:00-03:00") == datetime.fromisoformat(
        "2018-01-01T09:00:00-03:00")
