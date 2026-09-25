"""CLI run loop: reconnect after transport failures, per-case trace buffering, --resume."""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest

from student_agent import cli
from student_agent.evidence import CaseEvidenceStore, GatewayUnavailable

ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = [f"T_CASE_{i:03d}" for i in range(1, 101)]


def _output(case_id: str) -> dict:
    return {
        "schema_version": "day09-l3b-output-v2", "case_id": case_id,
        "assessment": {"primary_issue": "insufficient_evidence", "secondary_issues": [],
                       "case_status": "needs_investigation", "confidence": 0.3},
        "affected_entities": {"order_ids": [], "item_ids": [], "seller_ids": [],
                              "payment_references": [], "shipment_ids": []},
        "entity_resolution": {"status": "not_found", "resolved_order_ids": [],
                              "rejected_candidates": [], "confidence": 0.3},
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {"verdict": "insufficient_evidence", "late_seller_ids": [],
                              "timeline_complete": False},
        "payment_analysis": {"verdict": "insufficient_evidence", "captured_total_brl": None,
                             "refunded_total_brl": None, "refundable_total_brl": None},
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [], "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0,
                                 "refund_lines": []},
        "resolution_actions": ["escalate_for_investigation"],
    }


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    (tmp_path / "inputs").mkdir()
    (tmp_path / "case-set.json").write_text(json.dumps(
        {"case_set_version": "test-v1", "variant_id": "l3b", "case_ids": CASE_IDS}))
    for case_id in CASE_IDS:
        (tmp_path / "inputs" / f"{case_id}.json").write_text(json.dumps({"case_id": case_id}))
    monkeypatch.setenv("COMPETITION_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("COMPETITION_TEAM_API_KEY", "sk-team-" + "a" * 20)
    monkeypatch.setenv("MCP_ENDPOINT", "http://127.0.0.1:1/mcp")
    monkeypatch.setattr(cli, "RECONNECT_BACKOFF_SECONDS", 0)
    return tmp_path


def _patch(monkeypatch, fail_on: dict[str, int]) -> list[int]:
    """fail_on: case_id -> how many times solving it hits a dropped connection."""
    connections: list[int] = []

    class Gateway:
        async def list_tools(self):
            return ["get_order"]

    @asynccontextmanager
    async def fake_connect(*_args):
        connections.append(1)
        yield Gateway()

    async def fake_solve(case, gateway, trace):
        case_id = case["case_id"]
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
                   target="entity-agent")
        if fail_on.get(case_id, 0) > 0:
            fail_on[case_id] -= 1
            raise BaseExceptionGroup("session", [httpx2.ConnectError("dropped")])
        return _output(case_id)

    monkeypatch.setattr(cli, "connect_gateway", fake_connect)
    monkeypatch.setattr(cli, "solve_case", fake_solve)
    return connections


def _trace_events(root: Path) -> list[dict]:
    lines = (root / "traces" / "trace.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_reconnects_and_leaves_no_partial_trace(workspace: Path, monkeypatch) -> None:
    connections = _patch(monkeypatch, {"T_CASE_030": 2})
    asyncio.run(cli._run(workspace))
    assert len(connections) == 3
    assert len(list((workspace / "outputs").glob("*.json"))) == 100
    received = [e["case_id"] for e in _trace_events(workspace)
                if e["event_type"] == "case_received"]
    assert received == CASE_IDS  # each case exactly once, in order


def test_gives_up_after_max_reconnects(workspace: Path, monkeypatch) -> None:
    _patch(monkeypatch, {"T_CASE_005": cli.MAX_RECONNECTS + 1})
    with pytest.raises(BaseExceptionGroup):
        asyncio.run(cli._run(workspace))
    assert len(list((workspace / "outputs").glob("*.json"))) == 4


def test_resume_keeps_done_cases_and_drops_orphan_events(workspace: Path, monkeypatch) -> None:
    _patch(monkeypatch, {"T_CASE_005": cli.MAX_RECONNECTS + 1})
    with pytest.raises(BaseExceptionGroup):
        asyncio.run(cli._run(workspace))
    with (workspace / "traces" / "trace.jsonl").open("a") as handle:  # orphan from old runs
        handle.write(json.dumps({"case_id": "T_CASE_005", "event_type": "case_received"}) + "\n")
    connections = _patch(monkeypatch, {})
    asyncio.run(cli._run(workspace, resume=True))
    assert len(connections) == 1
    received = [e["case_id"] for e in _trace_events(workspace)
                if e["event_type"] == "case_received"]
    assert received == CASE_IDS


def test_non_connection_errors_are_not_retried(workspace: Path, monkeypatch) -> None:
    connections = _patch(monkeypatch, {})

    async def broken(case, gateway, trace):
        raise ValueError("bug")

    monkeypatch.setattr(cli, "solve_case", broken)
    with pytest.raises(ValueError):
        asyncio.run(cli._run(workspace))
    assert len(connections) == 1


def test_store_escalates_persistent_transport_failure(tmp_path: Path) -> None:
    class Down:
        async def call(self, tool_name, *, case_id, **arguments):
            raise httpx2.ConnectError("down")

    from student_agent.contracts import Contracts
    from student_agent.trace import TraceWriter

    trace = TraceWriter(tmp_path / "t.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    store = CaseEvidenceStore("CASE_001", Down(), trace)
    with pytest.raises(GatewayUnavailable):
        asyncio.run(store.fetch("entity-agent", "get_order", order_id="o1"))
