"""Agent の LLM 実装（fake / codex）とハンドラ契約（ADR-0043 §2・§6）。実 CLI は呼ばない。"""

from __future__ import annotations

import json

import pytest

from contracts.agents import AgentHandlerRef
from domain.agents.runtime import AGENT_INPUT_NAMES, AgentOutput
from domain.errors import ProviderInvocationError
from domain.script.ports import GenerationRequest, GenerationResult
from infrastructure.agents.codex_llm import CodexAgentLlm, parse_json_object, parse_token_usage
from infrastructure.agents.fake_llm import FAKE_MODEL, FakeAgentLlm


async def test_the_fake_is_deterministic_and_reports_tokens() -> None:
    llm = FakeAgentLlm({"brief": {"x": 1}})
    a = await llm.generate_json(prompt="make a brief", schema={}, max_tokens=10)
    b = await llm.generate_json(prompt="make a brief", schema={}, max_tokens=10)
    assert a == b
    assert a.model == FAKE_MODEL
    assert a.data == {"x": 1}
    assert a.input_tokens == 3 and a.output_tokens is not None
    assert len(llm.calls) == 2


async def test_the_fake_accepts_a_callable_and_never_returns_an_empty_answer() -> None:
    assert (
        await FakeAgentLlm(lambda p, s: {"echo": p}).generate_json(
            prompt="hi", schema={}, max_tokens=1
        )
    ).data == {"echo": "hi"}
    with pytest.raises(LookupError):
        await FakeAgentLlm({"other": {}}).generate_json(prompt="hi", schema={}, max_tokens=1)


class _Generator:
    def __init__(self, text: str, raw_log: str = "") -> None:
        self.text, self.raw_log = text, raw_log
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(
            text=self.text, provider_id="codex", model="m1", raw_log=self.raw_log
        )


async def test_the_codex_adapter_passes_the_schema_and_reads_usage_when_present() -> None:
    log = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 7}}
            ),
        ]
    )
    gen = _Generator('{"k": "v"}', log)
    llm = CodexAgentLlm(generator=gen, episode_id="e1", timeout_seconds=30)
    result = await llm.generate_json(prompt="P", schema={"type": "object"}, max_tokens=99)
    assert result.data == {"k": "v"}
    assert (result.model, result.input_tokens, result.output_tokens) == ("m1", 12, 7)
    request = gen.requests[0]
    assert request.output_schema == {"type": "object"}
    assert (request.episode_id, request.timeout_seconds, request.prompt) == ("e1", 30, "P")


async def test_missing_usage_stays_none_not_zero() -> None:
    llm = CodexAgentLlm(generator=_Generator("{}", "no events"), episode_id="e", timeout_seconds=1)
    result = await llm.generate_json(prompt="P", schema={}, max_tokens=1)
    assert (result.input_tokens, result.output_tokens) == (None, None)


@pytest.mark.parametrize(
    "usage", [{"input_tokens": "3"}, {"input_tokens": True, "output_tokens": -1}, [], None]
)
def test_unparseable_usage_is_none(usage) -> None:
    log = json.dumps({"type": "turn.completed", "usage": usage})
    assert parse_token_usage(log) == (None, None)


def test_json_output_may_be_fenced_but_must_be_an_object() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("not json", "[1]", '"s"'):
        with pytest.raises(ProviderInvocationError):
            parse_json_object(bad)


def test_handler_input_names_cover_every_registered_handler() -> None:
    assert set(AGENT_INPUT_NAMES) == set(AgentHandlerRef)
    assert AGENT_INPUT_NAMES[AgentHandlerRef.LEARNING_AGENT_V1] == (
        "upload_receipt",
        "measurements",
        "baseline",
    )


def test_an_output_needs_an_explicit_outcome() -> None:
    with pytest.raises(TypeError):
        AgentOutput(artifact=None, decision=None)  # type: ignore[call-arg]
    out = AgentOutput(artifact=None, decision=None, outcome="awaiting_public_metrics")
    assert out.proposals == () and out.findings == () and out.note is None
