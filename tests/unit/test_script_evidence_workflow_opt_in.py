"""ScriptWorkflow の Evidence の分岐（ADR-0038 §B6、INV-37）の replay 安全性と助言としての扱い。

守るもの:
- **OFF の履歴は接続前と同じ**: f209e7c の ``ScriptWorkflow`` で取った履歴
  （``tests/unit/fixtures/script_workflow_off_path_history.json``）が、OFF の ``ScriptWorkflow`` で
  も ON の ``EvidenceScriptWorkflow`` でも nondeterminism なしに replay できる。OFF で新しく走らせた
  履歴は fixture と同じ event・Activity の列で、patch の marker を持たない
- ON の新しい実行は marker を残し、台本の後・``script_mark_ready`` の前に 1 本の Activity を呼ぶ。
  その結果が何でも（照合 ``failed`` / 調査なし / timeout）、Activity が失敗しても
  ``script_ready`` で終わる（助言）
- ON で始まった履歴を OFF の worker が replay できる（設定を戻しても実行中の台本工程が止まらない）
- 台本が作れなかった実行では Evidence の Activity を呼ばない

fixture は f209e7c と同じ ``workers/planning/workflows.py``（B6 の変更前の HEAD 81fa6d6。``git diff
f209e7c 81fa6d6 -- workers/planning`` は空）で fake の Activity を相手に実行して取った（1 ラウンド目
は retryable な失敗、2 ラウンド目で成功）。旧 ``script_workflow_pre_0032_history.json`` は使わない
（visual style の型を含む。ADR-0037 §9）。理由は docs/testing/research-opt-in-rationale.md。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from workers.planning.activities import (
    CreateJobRequest,
    EpisodeRef,
    FailureOutcome,
    GenerateScriptRequest,
    RecordFailureRequest,
    ScriptResult,
)
from workers.planning.script_evidence import (
    SCRIPT_EVIDENCE_CHECK,
    SCRIPT_EVIDENCE_PATCH_ID,
    ScriptEvidenceOutcome,
    ScriptEvidenceRequest,
)
from workers.planning.workflows import EvidenceScriptWorkflow, ScriptWorkflow

OFF_HISTORY = Path(__file__).parent / "fixtures" / "script_workflow_off_path_history.json"
QUEUE = "script-b6-test"


@dataclass
class World:
    calls: list[str] = field(default_factory=list)
    fail_all_rounds: bool = False
    evidence: ScriptEvidenceOutcome | BaseException = field(
        default_factory=lambda: ScriptEvidenceOutcome(outcome="verified", verdict="failed")
    )
    evidence_requests: list[ScriptEvidenceRequest] = field(default_factory=list)


WORLD = World()


@activity.defn(name="script_mark_episode_in_progress")
async def mark_in_progress(request: EpisodeRef) -> None:
    WORLD.calls.append("script_mark_episode_in_progress")


@activity.defn(name="script_create_job")
async def create_job(request: CreateJobRequest) -> str:
    WORLD.calls.append("script_create_job")
    return "22222222-2222-4222-8222-222222222222"


@activity.defn(name="script_generate")
async def generate(request: GenerateScriptRequest) -> ScriptResult:
    WORLD.calls.append("script_generate")
    if WORLD.fail_all_rounds or request.round == 1:
        raise ApplicationError("unparseable", type="ScriptOutputUnparseableError")
    return ScriptResult(
        artifact_id="33333333-3333-4333-8333-333333333333",
        bucket="avp",
        object_key=f"episodes/{request.episode_id}/script/abc.json",
        sha256="a" * 64,
        schema_version="1",
        reused=False,
    )


@activity.defn(name="script_mark_ready")
async def mark_ready(request: EpisodeRef) -> str:
    WORLD.calls.append("script_mark_ready")
    return "script_ready"


@activity.defn(name="script_record_failure")
async def record_failure(request: RecordFailureRequest) -> FailureOutcome:
    WORLD.calls.append("script_record_failure")
    return FailureOutcome(episode_status="needs_work")


@activity.defn(name=SCRIPT_EVIDENCE_CHECK)
async def evidence_check(request: ScriptEvidenceRequest) -> ScriptEvidenceOutcome:
    WORLD.calls.append(SCRIPT_EVIDENCE_CHECK)
    WORLD.evidence_requests.append(request)
    if isinstance(WORLD.evidence, BaseException):
        raise WORLD.evidence
    return WORLD.evidence


BASE_ACTIVITIES = [mark_in_progress, create_job, generate, mark_ready, record_failure]


@pytest.fixture(autouse=True)
def _reset_world() -> None:
    global WORLD
    WORLD = World()


async def _run(workflow_cls: type, *, with_evidence_activity: bool = True) -> tuple[dict, Any]:
    episode = str(uuid.uuid4())
    activities = [*BASE_ACTIVITIES, *([evidence_check] if with_evidence_activity else [])]
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=QUEUE, workflows=[workflow_cls], activities=activities),
    ):
        handle = await env.client.start_workflow(
            "ScriptWorkflow",
            {"episode_id": episode},
            id=f"episode-{episode}",
            task_queue=QUEUE,
        )
        result = await handle.result()
        history = await handle.fetch_history()
    return result, history


def _shape(history: WorkflowHistory) -> list[tuple[str, str]]:
    """event の種類と Activity 名の列（id・時刻・payload を除いた履歴の形）。"""
    shape: list[tuple[str, str]] = []
    for event in json.loads(history.to_json())["events"]:
        kind = event["eventType"]
        scheduled = event.get("activityTaskScheduledEventAttributes")
        marker = event.get("markerRecordedEventAttributes")
        detail = scheduled["activityType"]["name"] if scheduled else ""
        detail = marker["markerName"] if marker else detail
        shape.append((kind, detail))
    return shape


def _markers(history: WorkflowHistory) -> list[str]:
    return [d for kind, d in _shape(history) if kind == "EVENT_TYPE_MARKER_RECORDED"]


def _off_history() -> WorkflowHistory:
    return WorkflowHistory.from_json("script-off-path", OFF_HISTORY.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ OFF


@pytest.mark.parametrize("workflow_cls", [ScriptWorkflow, EvidenceScriptWorkflow])
async def test_the_f209e7c_off_history_replays_on_both_workers(workflow_cls: type) -> None:
    await Replayer(workflows=[workflow_cls]).replay_workflow(_off_history())


async def test_a_new_off_run_has_the_same_history_shape_as_f209e7c() -> None:
    result, history = await _run(ScriptWorkflow)
    assert result["status"] == "script_ready"
    assert _shape(history) == _shape(_off_history())
    assert _markers(history) == []
    assert SCRIPT_EVIDENCE_CHECK not in WORLD.calls


async def test_the_off_worker_does_not_need_the_evidence_activity() -> None:
    result, _ = await _run(ScriptWorkflow, with_evidence_activity=False)
    assert result["status"] == "script_ready"


# ------------------------------------------------------------------ ON


@pytest.mark.parametrize(
    "evidence",
    [
        ScriptEvidenceOutcome(outcome="verified", verdict="passed"),
        ScriptEvidenceOutcome(outcome="verified", verdict="failed"),
        ScriptEvidenceOutcome(outcome="verified", verdict="insufficient"),
        ScriptEvidenceOutcome(outcome="no_research", research_status="partial"),
        ScriptEvidenceOutcome(outcome="no_research", research_status="blocked"),
        ScriptEvidenceOutcome(outcome="timeout"),
        ApplicationError("research gateway down", type="RuntimeError"),
    ],
    ids=["passed", "failed", "insufficient", "partial", "blocked", "timeout", "activity-fails"],
)
async def test_an_on_run_checks_evidence_once_and_always_reaches_script_ready(
    evidence: ScriptEvidenceOutcome | BaseException,
) -> None:
    WORLD.evidence = evidence
    result, history = await _run(EvidenceScriptWorkflow)

    assert result["status"] == "script_ready"
    assert result["rounds_used"] == 2
    calls = [c for c in WORLD.calls if c != "script_generate"]
    expected_checks = 2 if isinstance(evidence, BaseException) else 1  # retry は最大 2 回
    assert calls == [
        "script_mark_episode_in_progress",
        "script_create_job",
        *[SCRIPT_EVIDENCE_CHECK] * expected_checks,
        "script_mark_ready",
    ]
    assert _markers(history) == ["core_patch"]
    assert WORLD.evidence_requests[0].sha256 == "a" * 64
    # ON の履歴は OFF の worker でも ON の worker でも replay できる（設定を戻しても止まらない）
    for workflow_cls in (ScriptWorkflow, EvidenceScriptWorkflow):
        await Replayer(workflows=[workflow_cls]).replay_workflow(history)


async def test_an_on_run_records_the_b6_patch_id() -> None:
    _, history = await _run(EvidenceScriptWorkflow)
    patch_ids = [
        json.loads(base64.b64decode(payload["data"]))["id"]
        for event in json.loads(history.to_json())["events"]
        if "markerRecordedEventAttributes" in event
        for payload in event["markerRecordedEventAttributes"]["details"]["patch-data"]["payloads"]
    ]
    assert patch_ids == [SCRIPT_EVIDENCE_PATCH_ID]


async def test_no_evidence_is_checked_when_no_script_was_written() -> None:
    WORLD.fail_all_rounds = True
    result, history = await _run(EvidenceScriptWorkflow)
    assert result["status"] == "needs_work"
    assert SCRIPT_EVIDENCE_CHECK not in WORLD.calls
    assert _markers(history) == []
