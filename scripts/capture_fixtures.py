"""Capture raw MCP evidence for a few cases so agents can be developed offline.

Every call here is audited by the competition server; keep the case list short.
Fixtures contain competition data and are gitignored (tests/fixtures/evidence/).

Usage: python scripts/capture_fixtures.py [--core] L3B_CASE_001 L3B_CASE_004
  --core  skip sellers/product/policy/decoy lookups (already understood; saves audited calls)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "evidence"


def _save(case_id: str, name: str, payload: dict) -> None:
    target = OUT / case_id / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _capture(gateway, case_id: str, tool: str, name: str, **args: str) -> dict | None:
    try:
        evidence = await gateway.call(tool, case_id=case_id, **args)
    except (RuntimeError, ValueError) as exc:
        _save(case_id, name, {"tool": tool, "args": args, "error": str(exc)})
        print(f"  {name}: ERROR {exc}")
        return None
    _save(case_id, name, {"tool": tool, "args": args, "envelope": evidence})
    print(f"  {name}: {evidence['domain']} {evidence['evidence_ref'][:16]}...")
    return evidence


async def main(case_ids: list[str], core: bool) -> None:
    settings = Settings.load(ROOT)
    case_set = load_case_set(ROOT)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    order_tools = [
        "get_order", "get_order_items", "get_order_payments", "get_payment_timeline",
        "get_refund_timeline", "get_shipment_summary",
    ]
    if not core:
        order_tools += ["get_sellers", "get_product_context"]
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        for case_id in case_ids:
            case = case_set.cases[case_id]
            print(case_id)
            order_id = case["customer_request"]["claimed_order_id"]
            for tool in order_tools:
                await _capture(gw, case_id, tool, tool, order_id=order_id)
            await _capture(gw, case_id, "get_customer_history", "get_customer_history",
                           customer_unique_id=case["customer_unique_id_hint"])
            if core:
                continue
            await _capture(gw, case_id, "get_policy", "get_policy",
                           policy_version=case["policy_version"])
            for other in case["candidate_order_ids"]:
                if other != order_id:
                    await _capture(gw, case_id, "get_order", f"get_order__{other}", order_id=other)


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main([a for a in args if a != "--core"], "--core" in args))
