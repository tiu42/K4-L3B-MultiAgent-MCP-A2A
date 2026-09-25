"""In-process A2A messaging between agents, mirrored into the observable trace."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from .trace import TraceWriter

MAX_HOPS = 8
MAX_TRACE_REFS = 20

Intent = Literal["assign", "report", "request_info", "verify"]


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class A2AMessage:
    message_id: str
    case_id: str
    correlation_id: str
    sender: str
    recipient: str
    intent: Intent
    payload: dict[str, Any]
    evidence_refs: tuple[str, ...]
    hop: int


@dataclass
class Bus:
    """Routes messages for one case; rejects cross-case traffic, loops and re-assignment."""

    case_id: str
    trace: TraceWriter
    hop: int = 0
    log: list[A2AMessage] = field(default_factory=list)
    _assigned: set[tuple[str, str]] = field(default_factory=set)

    def _message(
        self,
        sender: str,
        recipient: str,
        intent: Intent,
        task: str,
        payload: dict[str, Any] | None,
        evidence_refs: Iterable[str],
    ) -> A2AMessage:
        self.hop += 1
        if self.hop > MAX_HOPS * 4:  # generous global guard; per-task guard below
            raise ProtocolError(f"{self.case_id}: message budget exceeded")
        message = A2AMessage(
            message_id=uuid.uuid4().hex,
            case_id=self.case_id,
            correlation_id=f"{self.case_id}:{task}",
            sender=sender,
            recipient=recipient,
            intent=intent,
            payload=payload or {},
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            hop=self.hop,
        )
        self.log.append(message)
        return message

    def assign(self, sender: str, recipient: str, task: str) -> A2AMessage:
        key = (recipient, task)
        if key in self._assigned:
            raise ProtocolError(f"{recipient} already assigned {task}")
        self._assigned.add(key)
        message = self._message(sender, recipient, "assign", task, None, ())
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=sender,
            target=recipient,
            decision_code=task.upper(),
        )
        return message

    def report(
        self,
        sender: str,
        recipient: str,
        task: str,
        status: str,
        payload: dict[str, Any] | None = None,
        evidence_refs: Iterable[str] = (),
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> A2AMessage:
        message = self._message(sender, recipient, "report", task, payload, evidence_refs)
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=sender,
            target=recipient,
            decision_code=status,
            evidence_refs=list(message.evidence_refs[:MAX_TRACE_REFS]) or None,
            attributes=attributes,
        )
        return message
