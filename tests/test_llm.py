"""LLM client tests with a fake OpenAI client (no network, no API key)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from student_agent.llm import MAX_CONSECUTIVE_FAILURES, LLMClient, strict_schema

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["topic"],
          "properties": {"topic": {"enum": ["duplicate_charge", "unknown"]}}}


class FakeCompletions:
    def __init__(self, content: str | None = None, refusal: str | None = None,
                 finish_reason: str = "stop", error: Exception | None = None) -> None:
        self.content, self.refusal, self.finish_reason, self.error = (
            content, refusal, finish_reason, error)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        message = SimpleNamespace(content=self.content, refusal=self.refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=message,
                                                        finish_reason=self.finish_reason)])


def _client(tmp_path: Path, completions: FakeCompletions) -> LLMClient:
    client = LLMClient(tmp_path, enabled=False)
    client.enabled = True
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client


def _ask(client: LLMClient, text: str = "charged twice"):
    return asyncio.run(client.ask("intake-agent", "classify_claim",
                                  {"untrusted_customer_text": text}, SCHEMA))


def test_strict_schema_adds_string_type_to_enums() -> None:
    adapted = strict_schema(SCHEMA)
    assert adapted["properties"]["topic"]["type"] == "string"
    assert "type" not in SCHEMA["properties"]["topic"]  # caller's schema untouched


def test_request_shape_and_disk_cache(tmp_path: Path) -> None:
    completions = FakeCompletions(content=json.dumps({"topic": "duplicate_charge"}))
    client = _client(tmp_path, completions)
    assert _ask(client) == {"topic": "duplicate_charge"}
    request = completions.requests[0]
    assert request["model"] == "gpt-4o-mini" and request["temperature"] == 0
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["messages"][0]["role"] == "system"
    assert _ask(client) == {"topic": "duplicate_charge"}
    assert len(completions.requests) == 1  # second answer served from .llm_cache


def test_refusal_and_truncation_return_none(tmp_path: Path) -> None:
    assert _ask(_client(tmp_path / "a", FakeCompletions(refusal="no"))) is None
    truncated = FakeCompletions(content="{", finish_reason="length")
    assert _ask(_client(tmp_path / "b", truncated)) is None


def test_repeated_failures_disable_llm(tmp_path: Path) -> None:
    completions = FakeCompletions(error=ConnectionError("down"))
    client = _client(tmp_path, completions)
    for i in range(MAX_CONSECUTIVE_FAILURES + 2):
        assert _ask(client, f"text {i}") is None
    assert not client.enabled
    assert len(completions.requests) == MAX_CONSECUTIVE_FAILURES


def test_missing_api_key_means_rule_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not LLMClient(tmp_path, enabled=True).enabled


def test_actor_permission(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeCompletions(content="{}"))
    with pytest.raises(PermissionError):
        asyncio.run(client.ask("verifier", "adjudicate", {}, SCHEMA))
