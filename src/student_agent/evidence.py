"""Per-case evidence store: permission check, cache, budget, bounded retry and trace linkage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from .trace import TraceWriter

# Least privilege: which actor may call which MCP tool. New tools from discovery are not
# usable until they are added here on purpose.
ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"get_customer_history", "get_order"}),
    "order-agent": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-agent": frozenset(
        {"get_payment_timeline", "get_refund_timeline", "get_order_payments"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}

DEFAULT_CALL_BUDGET = 12
TRANSIENT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 2.0
MAX_CONCURRENT_CALLS = 4


class Gateway(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]: ...


class ToolPermissionError(PermissionError):
    pass


class GatewayUnavailable(ConnectionError):
    """Transport kept failing: the session is likely dead; the caller should reconnect."""


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool: str
    args: tuple[tuple[str, str], ...]
    domain: str
    data: Any
    warnings: tuple[str, ...]
    result_hash: str


@dataclass(frozen=True)
class ToolFailure:
    tool: str
    args: tuple[tuple[str, str], ...]
    code: str  # TOOL_ERROR | TOOL_UNAVAILABLE | INVALID_EVIDENCE | BUDGET_EXHAUSTED
    message: str


@dataclass
class CaseEvidenceStore:
    case_id: str
    gateway: Gateway
    trace: TraceWriter
    budget: int = DEFAULT_CALL_BUDGET
    calls_made: int = 0
    evidence: dict[str, Evidence] = field(default_factory=dict)
    failures: list[ToolFailure] = field(default_factory=list)
    _cache: dict[tuple, Evidence | ToolFailure] = field(default_factory=dict)
    _locks: dict[tuple, asyncio.Lock] = field(default_factory=dict)
    _semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    )

    def owns(self, ref: str) -> bool:
        return ref in self.evidence

    def refs_for(self, *tools: str) -> list[str]:
        return [ev.ref for ev in self.evidence.values() if ev.tool in tools]

    async def fetch(self, actor: str, tool: str, **args: str) -> Evidence | None:
        """Return evidence for (tool, args) once per case; None when unavailable."""
        if tool not in ALLOWED_TOOLS.get(actor, frozenset()):
            raise ToolPermissionError(f"{actor} is not allowed to call {tool}")
        key = (tool, tuple(sorted(args.items())))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached if isinstance(cached, Evidence) else None
            result = await self._call(tool, key[1], args)
            self._cache[key] = result
        if isinstance(result, ToolFailure):
            self.failures.append(result)
            return None
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[result.ref],
            attributes={"domain": result.domain, "warnings": len(result.warnings)},
        )
        return result

    async def _call(
        self, tool: str, key_args: tuple[tuple[str, str], ...], args: dict[str, str]
    ) -> Evidence | ToolFailure:
        attempts = 0
        while True:
            if self.calls_made >= self.budget:
                return ToolFailure(tool, key_args, "BUDGET_EXHAUSTED", "per-case call budget")
            self.calls_made += 1
            attempts += 1
            try:
                async with self._semaphore:
                    envelope = await self.gateway.call(tool, case_id=self.case_id, **args)
            except RuntimeError as exc:  # MCP isError: deterministic, never retried
                return ToolFailure(tool, key_args, "TOOL_ERROR", str(exc)[:160])
            except ValueError as exc:  # envelope failed the public contract
                return ToolFailure(tool, key_args, "INVALID_EVIDENCE", str(exc)[:160])
            except Exception as exc:  # transport / timeout: bounded, idempotent retry
                if attempts > TRANSIENT_RETRIES:
                    # Do not silently degrade the case: let the runner reconnect and redo it.
                    raise GatewayUnavailable(f"{tool}: {exc!r}"[:200]) from exc
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                continue
            evidence = Evidence(
                ref=envelope["evidence_ref"],
                tool=tool,
                args=key_args,
                domain=envelope["domain"],
                data=envelope["data"],
                warnings=tuple(envelope.get("warnings") or ()),
                result_hash=envelope["result_hash"],
            )
            self.evidence[evidence.ref] = evidence
            return evidence
