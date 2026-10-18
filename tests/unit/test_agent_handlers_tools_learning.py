"""learning_agent_v1 の spec 1.1（Review の役。ADR-0047 §2/§3、INV-57 / INV-58 / INV-60）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.agent_tools import ToolId, ToolStatus
from contracts.agentic import Lesson, LessonStatus
from contracts.intervention import AgentRole
from domain.agents.handlers import HANDLERS
from domain.agents.runtime import AgentOutput
from domain.errors import ToolNotAllowedError
from tests.support.agent_tools_handlers import (
    OTHER_VIDEO,
    VIDEO,
    ScriptedLlm,
    agent_input,
    make_gateway,
    ok,
    status_only,
    video_output,
)

UNKNOWN_ID = "00000000-0000-4000-8000-00000000dead"
PLAN = {
    "requests": [
        {"tool_ref": "get_video_analytics@1", "input": {"video_id": VIDEO}},
        {"tool_ref": "get_audience_retention@1", "input": {"video_id": VIDEO}},
    ]
}


def _docs(**over: Any) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        "upload_receipt": {"video_id": VIDEO, "privacy_status": "private"},
        "measurements": {
            "public": True,
            "format": "shorts",
            "published_at": "2026-10-01T00:00:00+00:00",
            "windows": [{"window": "7d", "views": 900, "average_view_percentage": 61.5}],
        },
        "baseline": {"format": "shorts", "comparable": True, "description": "last 10 Shorts"},
    }
    docs.update(over)
    return docs


def _answer(**over: Any) -> dict[str, Any]:
    a: dict[str, Any] = {
        "observations": ["7d views were 900."],
        "hypotheses": ["The hook may have helped (see tool_call:{id})."],
        "alternative_explanations": ["Timing could explain it."],
        "proposed_experiments": ["Try a different hook."],
    }
    a.update(over)
    return a


def _executors(**over: Any):
    base: dict[ToolId, Any] = {
        ToolId.GET_VIDEO_ANALYTICS: ok(video_output(), "analytics_query:v1"),
        ToolId.GET_AUDIENCE_RETENTION: status_only(ToolStatus.NO_DATA),
    }
    base.update(over)  # type: ignore[call-overload]  # key は ToolId.value の文字列
    return base


async def _run(
    llm: ScriptedLlm, executors, docs=None, *, runtime: bool = True
) -> tuple[AgentOutput, Any, Any]:
    gateway, store = make_gateway(AgentRole.LEARNING, "1.1", llm, executors, runtime=runtime)
    inp = agent_input(AgentRole.LEARNING, "1.1", docs or _docs())
    return await HANDLERS[inp.spec.handler_ref](inp, gateway), gateway, store


def _ids(gateway) -> list[str]:
    return [i["tool_call_id"] for i in gateway.invocations() if i.get("tool_call_id")]


async def _real_id() -> str:
    """tool_call_id は run の冪等キー・順序・入力から決まる。同じ計画を走らせて実 ID を知る。"""
    _, gateway, _ = await _run(ScriptedLlm(PLAN, _answer(hypotheses=[])), _executors())
    return _ids(gateway)[0]


def _with_id(answer: dict[str, Any], real: str) -> dict[str, Any]:
    return {k: [t.replace("{id}", real) for t in v] for k, v in answer.items()}


# --------------------------------------------------------------------------- 往復


async def test_a_tool_round_proposes_a_lesson_whose_hypotheses_cite_real_tool_calls() -> None:
    real = await _real_id()
    llm = ScriptedLlm(PLAN, _with_id(_answer(), real))
    out, gateway, store = await _run(llm, _executors())
    lesson = out.artifact
    assert isinstance(lesson, Lesson)
    assert lesson.status is LessonStatus.PROPOSED and out.outcome == "produced"
    assert f"tool_call:{real}" in lesson.hypotheses[0]
    assert llm.calls == 2 and store.begins == 2
    decision = out.decision
    assert decision is not None
    assert f"tool_call:{real}" in decision.evidence_refs
    assert "analytics_query:v1" in decision.evidence_refs
    # 測定窓・比較の基準は決定論のまま（LLM も tool も上書きしない）
    assert lesson.measurement_windows[0].views == 900
    assert lesson.comparison_basis is not None and "last 10 Shorts" in lesson.comparison_basis


async def test_unknown_citations_are_removed_and_uncited_hypotheses_are_dropped() -> None:
    real = await _real_id()
    answer = _with_id(
        _answer(
            hypotheses=[
                "Cited hypothesis (tool_call:{id}).",
                "Invented source (tool_call:" + UNKNOWN_ID + ").",
                "No citation at all.",
            ],
            observations=["Views were 900 (tool_call:" + UNKNOWN_ID + ")."],
        ),
        real,
    )
    out, _, _ = await _run(ScriptedLlm(PLAN, answer), _executors())
    lesson = out.artifact
    assert isinstance(lesson, Lesson)
    # tool の証拠があるのに引用が無い／実在しない仮説は残さない
    assert lesson.hypotheses == (f"Cited hypothesis (tool_call:{real}).",)
    assert UNKNOWN_ID not in " ".join(lesson.observations)
    assert lesson.sample_note is not None and "dropped" in lesson.sample_note


async def test_without_any_ok_tool_data_the_lesson_says_it_is_not_grounded() -> None:
    denied = {
        ToolId.GET_VIDEO_ANALYTICS: status_only(ToolStatus.PERMISSION_DENIED),
        ToolId.GET_AUDIENCE_RETENTION: status_only(ToolStatus.QUOTA_EXCEEDED),
    }
    out, _, _ = await _run(
        ScriptedLlm(PLAN, _answer(hypotheses=["A plain hypothesis."])), _executors(**denied)
    )
    lesson = out.artifact
    assert isinstance(lesson, Lesson) and lesson.status is LessonStatus.PROPOSED
    assert lesson.hypotheses == ("A plain hypothesis.",)  # 仮説は残るが根拠付きとは言わない
    assert lesson.sample_note is not None and "not grounded" in lesson.sample_note
    assert out.decision is not None
    assert any("permission_denied" in u for u in out.decision.uncertainties)
    assert lesson.measurement_windows[0].views == 900  # 欠測を作らず、既存の測定窓のまま


# --------------------------------------------------------------------------- INV-57 を保つ


@pytest.mark.parametrize(
    "measurements",
    [
        {"public": False, "windows": [{"window": "48h", "views": 5000}]},
        {"windows": [{"window": "48h", "views": 5000}]},
        {"public": True, "windows": []},
    ],
)
async def test_private_or_unmeasured_uploads_wait_without_llm_or_tools(measurements) -> None:
    llm = ScriptedLlm(PLAN, _answer())
    executors = _executors()
    out, gateway, store = await _run(llm, executors, _docs(measurements=measurements))
    assert out.artifact is None and out.outcome == "awaiting_public_metrics"
    assert llm.calls == 0 and store.begins == 0 and gateway.invocations() == []
    assert all(not e.calls for e in executors.values())


async def test_tools_are_limited_to_the_uploaded_video() -> None:
    plan = {
        "requests": [
            {"tool_ref": "get_video_analytics@1", "input": {"video_id": OTHER_VIDEO}},
            {"tool_ref": "get_audience_retention@1", "input": {"video_id": OTHER_VIDEO}},
            {
                "tool_ref": "compare_video_performance@1",
                "input": {"video_ids": [OTHER_VIDEO, "cbcdefghijk"]},
            },
        ]
    }
    executors = _executors(
        **{ToolId.COMPARE_VIDEO_PERFORMANCE: status_only(ToolStatus.INSUFFICIENT_DATA)}
    )
    out, _, store = await _run(ScriptedLlm(plan, _answer(hypotheses=[])), executors)
    assert isinstance(out.artifact, Lesson) and store.begins == 0
    assert all(not e.calls for e in executors.values())  # 他の動画の分析を読まない
    assert out.decision is not None
    assert any("refused" in u for u in out.decision.uncertainties)


async def test_comparison_must_include_the_uploaded_video() -> None:
    plan = {
        "requests": [
            {
                "tool_ref": "compare_video_performance@1",
                "input": {"video_ids": [VIDEO, OTHER_VIDEO]},
            }
        ]
    }
    executors = _executors(
        **{ToolId.COMPARE_VIDEO_PERFORMANCE: status_only(ToolStatus.INSUFFICIENT_DATA)}
    )
    out, _, store = await _run(ScriptedLlm(plan, _answer(hypotheses=[])), executors)
    assert isinstance(out.artifact, Lesson) and store.begins == 1
    assert out.decision is not None
    assert any("insufficient_data" in u for u in out.decision.uncertainties)


async def test_a_baseline_of_another_format_is_still_never_mixed() -> None:
    docs = _docs(baseline={"format": "long_form", "comparable": True, "description": "long videos"})
    out, _, _ = await _run(
        ScriptedLlm({"requests": []}, _answer(hypotheses=[])), _executors(), docs
    )
    lesson = out.artifact
    assert isinstance(lesson, Lesson)
    assert (
        lesson.comparison_basis is not None and "formats are not mixed" in lesson.comparison_basis
    )


async def test_without_a_tool_runtime_the_1_1_spec_cannot_run() -> None:
    llm = ScriptedLlm(PLAN, _answer())
    with pytest.raises(ToolNotAllowedError):
        await _run(llm, _executors(), runtime=False)
    assert llm.calls == 0


async def test_a_synthesis_violation_is_repaired_once() -> None:
    llm = ScriptedLlm(PLAN, {"observations": "nope"}, _answer(hypotheses=[]))
    out, _, _ = await _run(llm, _executors())
    assert isinstance(out.artifact, Lesson) and llm.calls == 3
