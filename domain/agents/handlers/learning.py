"""learning_agent_v1 → Lesson または awaiting_public_metrics（ADR-0043 §6、設計 §9）。

公開されていない・測定が全く無い動画からは Lesson を作らない（LLM も呼ばない）。
測定窓・status・標本注記・比較の基準は決定論で決め、LLM は観測と仮説の文章だけを書く。
欠けた指標は ``None``（0 にしない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from contracts.agentic import Lesson, LessonStatus, MeasurementWindow
from contracts.intervention import DecisionCandidate, DecisionRecordPayload, RationaleKind
from domain.agents.handlers._support import ask, build_prompt, derive_model, document, validated
from domain.agents.runtime import AgentInput, AgentOutput
from domain.agents.tool_gateway import ToolGateway

#: この views 未満の測定は小標本として注記する（断定しない）
SMALL_SAMPLE_VIEWS = 100

MISSING_REASON_DEFAULT = "metrics were not available when the window was fetched"

_Response = derive_model(
    "LessonResponse",
    Lesson,
    include=("observations", "hypotheses", "alternative_explanations", "proposed_experiments"),
)


def _windows(measurements: dict[str, Any]) -> tuple[MeasurementWindow, ...]:
    windows: list[MeasurementWindow] = []
    for raw in measurements.get("windows", ()):
        item = dict(raw)
        metrics = (
            "views",
            "subscribers_gained",
            "average_view_duration_seconds",
            "average_view_percentage",
        )
        if all(item.get(m) is None for m in metrics) and not item.get("missing_reason"):
            item["missing_reason"] = MISSING_REASON_DEFAULT
        windows.append(validated(MeasurementWindow, item, "measurement window"))
    return tuple(windows)


def _has_metrics(w: MeasurementWindow) -> bool:
    return any(
        m is not None
        for m in (
            w.views,
            w.subscribers_gained,
            w.average_view_duration_seconds,
            w.average_view_percentage,
        )
    )


def _comparison(baseline: dict[str, Any], measured_format: Any) -> tuple[str, str]:
    """(scope, comparison_basis)。Shorts と長尺の基準を混ぜない。"""
    fmt = baseline.get("format")
    scope_format = fmt or measured_format or "unspecified format"
    scope = f"This video only ({scope_format}); one observation, not a general rule."
    if not baseline.get("comparable") or not baseline.get("description"):
        return scope, "No comparable baseline exists for this format; compare nothing."
    if measured_format and fmt and fmt != measured_format:
        return scope, (
            f"No comparable baseline: the baseline is {fmt} but this video is {measured_format}; "
            "formats are not mixed."
        )
    return scope, f"{fmt or 'same-format'} baseline: {baseline['description']}"


def _awaiting(note: str) -> AgentOutput:
    decision = DecisionRecordPayload(
        decision_type="lesson",
        question="Can a lesson be drawn from this upload yet?",
        candidates=(DecisionCandidate(id="awaiting_public_metrics", summary=note),),
        rationale_kind=RationaleKind.RULE,
        rationale=note,
    )
    return AgentOutput(
        artifact=None, decision=decision, outcome="awaiting_public_metrics", note=note
    )


_NOT_PUBLIC = "The video is not public, so viewer metrics do not exist yet."
_NO_METRICS = "No public metrics have been measured yet."


def _awaiting_note(
    measurements: dict[str, Any],
) -> tuple[str | None, tuple[MeasurementWindow, ...]]:
    """(待つ理由, windows)。理由が ``None`` のときだけ LLM が要る（決定論の事前判定）。"""
    if measurements.get("public") is not True:
        return _NOT_PUBLIC, ()
    windows = _windows(measurements)
    if not any(_has_metrics(w) for w in windows):
        return _NO_METRICS, windows
    return None, windows


def learning_needs_llm(documents: Mapping[str, Mapping[str, Any]]) -> bool:
    """LLM を呼ぶ経路か。``False`` なら予算を予約しない（呼ばないものに課金枠を取らない）。

    判定できないとき（入力が壊れている）は ``True``（従来どおり予約して handler に任せる）。
    """
    try:
        note, _ = _awaiting_note(dict(documents.get("measurements", {})))
    except Exception:
        return True
    return note is None


async def learning_agent_v1(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
    receipt = dict(document(agent_input, "upload_receipt"))
    measurements = dict(document(agent_input, "measurements"))
    baseline = dict(document(agent_input, "baseline"))

    note, windows = _awaiting_note(measurements)
    if note is not None:
        return _awaiting(note)

    scope, basis = _comparison(baseline, measurements.get("format"))
    views = [w.views for w in windows if w.views is not None]
    sample_note = None
    if not views:
        sample_note = "Views are unknown for every window; treat any reading as very uncertain."
    elif max(views) < SMALL_SAMPLE_VIEWS:
        sample_note = (
            f"Small sample: at most {max(views)} views (< {SMALL_SAMPLE_VIEWS}); "
            "differences are likely noise."
        )

    prompt = build_prompt(
        "Write observations (what the numbers show), hypotheses (possible causes, never stated as "
        "fact), alternative explanations and proposed experiments for this video. Do not claim "
        "causation. Missing metrics are unknown, not zero.",
        {
            "upload_receipt": receipt,
            "measurements": {
                **measurements,
                "windows": [w.model_dump(mode="json") for w in windows],
            },
            "baseline": baseline,
            "comparison_basis": basis,
        },
    )
    response = await ask(gateway, prompt, _Response)
    lesson = validated(
        Lesson,
        {
            "episode_id": agent_input.episode_id,
            "video_id": receipt.get("video_id"),
            "manifest_id": receipt.get("manifest_id"),
            "published_at": measurements.get("published_at"),
            "measurement_windows": [w.model_dump() for w in windows],
            "observations": response.observations,
            "hypotheses": response.hypotheses,
            "alternative_explanations": response.alternative_explanations,
            "proposed_experiments": response.proposed_experiments,
            "scope": scope,
            "status": LessonStatus.PROPOSED,
            "comparison_basis": basis,
            "sample_note": sample_note,
        },
        "lesson",
    )
    decision = DecisionRecordPayload(
        decision_type="lesson",
        question="What can be learned from this video's public metrics?",
        candidates=(
            DecisionCandidate(
                id="lesson_proposed",
                summary="A proposed lesson; not validated until tested.",
                concerns=(sample_note,) if sample_note else (),
            ),
        ),
        selected_candidate="lesson_proposed",
        rationale_kind=RationaleKind.REPORTED,
        rationale=basis,
        uncertainties=(sample_note,) if sample_note else (),
    )
    return AgentOutput(artifact=lesson, decision=decision, outcome="produced")
