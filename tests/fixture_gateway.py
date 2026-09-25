"""Offline stand-in for the MCP gateway, replaying evidence saved by scripts/capture_fixtures.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "evidence"


def fixture_cases() -> list[str]:
    if not FIXTURES.is_dir() or not (ROOT / "inputs").is_dir():
        return []
    return sorted(p.name for p in FIXTURES.iterdir()
                  if p.is_dir() and (ROOT / "inputs" / f"{p.name}.json").exists())


def load_case(case_id: str) -> dict[str, Any]:
    return json.loads((ROOT / "inputs" / f"{case_id}.json").read_text(encoding="utf-8"))


class FixtureGateway:
    """Serves saved envelopes by (case, tool, args); unknown calls behave like MCP errors."""

    def __init__(self, rename: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.rename = rename or {}
        self._index: dict[tuple[str, str, tuple], dict[str, Any]] = {}
        for path in FIXTURES.glob("*/*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            key = (path.parent.name, record["tool"], tuple(sorted(record["args"].items())))
            self._index[key] = record

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((case_id, tool_name, arguments))
        args_key = tuple(sorted(arguments.items()))
        record = self._index.get((case_id, tool_name, args_key))
        if record is None and tool_name == "get_policy":
            # Offline only: the policy document is identical across cases (same result_hash),
            # so cases captured with --core reuse any saved copy for the same version.
            record = next((r for (_, t, a), r in self._index.items()
                           if t == tool_name and a == args_key and "envelope" in r), None)
        if record is None or "error" in record:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        envelope = json.loads(json.dumps(record["envelope"]))
        if self.rename:
            envelope["data"] = _rename(envelope["data"], self.rename)
        return envelope


def _rename(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {mapping.get(k, k): _rename(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename(v, mapping) for v in value]
    return value
