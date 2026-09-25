from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..a2a import Bus
from ..evidence import CaseEvidenceStore
from ..llm import LLMClient


@dataclass
class Claim:
    claim_id: str
    topic: str  # a known topic, or "unknown"
    source: str  # input | llm | unknown


@dataclass
class IntakeReport:
    claims: list[Claim]
    claimed_order_id: str | None
    candidates: list[str]
    customer_hint: str | None
    opened_at: datetime | None
    policy_version: str | None
    scope: dict[str, bool]

    @property
    def issue_topics(self) -> list[str]:
        return [c.topic for c in self.claims if c.topic not in {"requested_full_refund", "unknown"}]


@dataclass
class EntityReport:
    status: str  # resolved | ambiguous | not_found
    order_id: str | None
    rejected: dict[str, str] = field(default_factory=dict)  # candidate -> reason code
    confidence: float = 0.0
    customer_unique_id: str | None = None
    related_order_ids: list[str] = field(default_factory=list)
    history_rows: list[dict[str, Any]] = field(default_factory=list)
    order_row: dict[str, Any] | None = None


@dataclass
class CaseContext:
    case: dict[str, Any]
    store: CaseEvidenceStore
    bus: Bus
    llm: LLMClient

    @property
    def case_id(self) -> str:
        return self.case["case_id"]
