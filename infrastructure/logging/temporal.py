"""Temporal の Activity interceptor（log-contract §5・§8 / INV-38）。

Activity の実行の周りで、activity info と**入力型ごとの明示の対応表**から文脈を束縛し、
``activity.started``（DEBUG）/ ``activity.succeeded`` / ``activity.failed`` を出す。

- 属性名で汎用的に拾わない。Research 入力の ``request_id`` は依頼 ID なので
  ``research_request_id`` へ写す（API の ``request_id`` と混ぜない）。
- 音声の ``scene_id`` は台本のシーン ID（音声 Artifact の ``scene_id`` と同じ名前空間）。
- 失敗・cancel は記録してから**同じ例外オブジェクト**を bare ``raise`` で再送出する。包み直さない。
- Workflow には interceptor を付けない（Workflow は ``workflow.logger`` だけ。INV-40）。

入力型は ``workers`` にもあるので型そのものは import せず、``<module>.<qualname>`` で引く。
新しい Activity の入力型がこの表に無いことは unit test が検出する。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from contracts.log_contract import EventName, Outcome
from domain.errors import classify_failure, failure_class_from_type_name
from infrastructure.logging.context import log_context
from infrastructure.logging.emit import emit
from infrastructure.logging.formatter import exception_type_name

logger = logging.getLogger("avp.activity")

_E = "episode_id"
_S = "scene_id"
_J = "job_id"
_SB = "storyboard_artifact_id"
_R = "reservation_id"

#: 入力型 → {入力の属性名: ログのフィールド名}。``attributes.<名前>`` は検索対象外の補助情報。
ACTIVITY_INPUT_FIELDS: dict[str, Mapping[str, str]] = {
    # production（画像・動画・音声・代替案・工程の状態）
    "contracts.production_activities.ImageSubmitRequest": {_E: _E, _S: _S, _SB: _SB},
    "contracts.production_activities.ImageAwaitRequest": {_E: _E, _S: _S, _SB: _SB, _R: _R},
    "contracts.production_activities.VideoSubmitRequest": {_E: _E, _S: _S, _SB: _SB},
    "contracts.production_activities.VideoAwaitRequest": {_E: _E, _S: _S, _SB: _SB, _R: _R},
    "contracts.production_activities.VoiceGenerateRequest": {
        _E: _E,
        "script_scene_id": _S,
        _SB: _SB,
        "storyboard_scene_ids": "attributes.storyboard_scene_ids",
    },
    "contracts.production_activities.PlanSceneAlternativeRequest": {_E: _E, _S: _S, _SB: _SB},
    "contracts.production_activities.ProductionAdmitRequest": {_E: _E},
    "contracts.production_activities.ProductionPlanRequest": {_E: _E},
    "contracts.production_activities.ProductionAssembleRequest": {_E: _E, _SB: _SB},
    "contracts.production_activities.ProductionMarkReadyRequest": {_E: _E},
    "contracts.production_activities.ProductionRecordFailureRequest": {_E: _E, _J: _J},
    # render / upload
    "contracts.render_activities.RenderAdmitRequest": {_E: _E},
    "contracts.render_activities.RenderFinalVideoRequest": {_E: _E},
    "contracts.render_activities.RenderMarkReadyRequest": {_E: _E},
    "contracts.render_activities.RenderRecordFailureRequest": {_E: _E, _J: _J},
    "contracts.upload_activities.UploadAdmitRequest": {_E: _E},
    "contracts.upload_activities.UploadFinalVideoRequest": {_E: _E},
    "contracts.upload_activities.UploadAwaitProcessingRequest": {
        _E: _E,
        "video_id": "attributes.video_id",
    },
    "contracts.upload_activities.UploadMarkUploadedRequest": {_E: _E},
    "contracts.upload_activities.UploadRecordFailureRequest": {_E: _E, _J: _J},
    # pipeline（日次枠・停止・投稿の可否・watchdog）
    "contracts.pipeline.CheckPausedRequest": {},
    "contracts.pipeline.ClaimDailySlotRequest": {"slot_date": "attributes.slot_date"},
    "contracts.pipeline.UploadGateRequest": {_E: _E},
    "contracts.schedule_guard.WatchdogCheckRequest": {},
    # planning（台本・企画・台本の根拠確認）
    "workers.planning.activities.EpisodeRef": {_E: _E},
    "workers.planning.activities.CreateJobRequest": {_E: _E},
    "workers.planning.activities.GenerateScriptRequest": {_E: _E, _J: _J},
    "workers.planning.activities.RecordFailureRequest": {_E: _E, _J: _J},
    "workers.planning.script_evidence.ScriptEvidenceRequest": {_E: _E},
    "contracts.topic_planning.FindPlanRequest": {"plan_date": "attributes.plan_date"},
    "contracts.topic_planning.TopicPlannerInput": {"plan_date": "attributes.plan_date"},
    "contracts.topic_planning.GenerateCandidatesRequest": {},
    "contracts.topic_planning.SelectAndSaveRequest": {},
    # storyboard
    "workers.storyboard.activities.AdmitRequest": {_E: _E},
    "workers.storyboard.activities.CreateJobRequest": {_E: _E},
    "workers.storyboard.activities.MarkReadyRequest": {_E: _E},
    "workers.storyboard.activities.GenerateStoryboardRequest": {_E: _E, _J: _J},
    "workers.storyboard.activities.RecordFailureRequest": {_E: _E, _J: _J},
    # dummy（骨組み）
    "workers.dummy.activities.EpisodeRef": {_E: _E},
    "workers.dummy.activities.CreateJobRequest": {_E: _E},
    "workers.dummy.activities.ProduceArtifactRequest": {_E: _E, _J: _J},
    "workers.dummy.activities.RecordFailureRequest": {_E: _E, _J: _J},
    # research（request_id は依頼 ID。API の request_id ではない）
    "contracts.research.ResearchExecuteRequest": {"request_id": "research_request_id"},
    "contracts.research.ResearchRecordFailureRequest": {"request_id": "research_request_id"},
}


def input_fields(arg: Any) -> dict[str, Any]:
    """入力1つから束縛するフィールド。表に無い型は何も拾わない。"""
    cls = type(arg)
    mapping = ACTIVITY_INPUT_FIELDS.get(f"{cls.__module__}.{cls.__qualname__}")
    if not mapping:
        return {}
    out: dict[str, Any] = {}
    attributes: dict[str, Any] = {}
    for attr, field in mapping.items():
        value = getattr(arg, attr, None)
        if value is None or value == "":
            continue
        if field.startswith("attributes."):
            attributes[field.removeprefix("attributes.")] = value
        else:
            out[field] = value
    if attributes:
        out["attributes"] = attributes
    return out


def _failure_fields(exc: BaseException) -> dict[str, Any]:
    error_type = exception_type_name(exc)
    if type(exc).__name__ == "ApplicationError":
        failure_class = failure_class_from_type_name(error_type)
    else:
        failure_class = classify_failure(exc)
    return {
        "outcome": Outcome.FAILED.value,
        "error_type": error_type,
        "error_message": str(exc),
        "failure_class": failure_class.value,
        # Temporal がこの Activity を再試行し得るか（log-contract §2）
        "retryable": not bool(getattr(exc, "non_retryable", False)),
    }


class _LoggingActivityInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        fields: dict[str, Any] = {}
        try:
            info = activity.info()
            fields = {
                "activity_id": info.activity_id,
                "activity_type": info.activity_type,
                "activity_attempt": info.attempt,
                "task_queue": info.task_queue,
                "workflow_id": info.workflow_id,
                "run_id": info.workflow_run_id,
                "workflow_type": info.workflow_type,
            }
            if input.args:
                fields.update(input_fields(input.args[0]))
        except Exception:  # 文脈が取れなくても Activity は実行する（INV-38）
            pass
        activity_type = fields.get("activity_type", "?")
        started = time.monotonic()
        with log_context(**fields):
            emit(
                logger,
                EventName.ACTIVITY_STARTED,
                logging.DEBUG,
                "activity %s started",
                activity_type,
                outcome=Outcome.STARTED.value,
            )
            try:
                result = await super().execute_activity(input)
            except asyncio.CancelledError:
                emit(
                    logger,
                    EventName.ACTIVITY_FAILED,
                    logging.INFO,
                    "activity %s cancelled",
                    activity_type,
                    outcome=Outcome.CANCELLED.value,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                raise
            except BaseException as exc:
                try:
                    failure = _failure_fields(exc)
                except Exception:
                    failure = {"outcome": Outcome.FAILED.value}
                emit(
                    logger,
                    EventName.ACTIVITY_FAILED,
                    logging.WARNING,
                    "activity %s failed",
                    activity_type,
                    duration_ms=(time.monotonic() - started) * 1000,
                    **failure,
                )
                raise
            emit(
                logger,
                EventName.ACTIVITY_SUCCEEDED,
                logging.INFO,
                "activity %s succeeded",
                activity_type,
                outcome=Outcome.SUCCEEDED.value,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return result


class ActivityLoggingInterceptor(Interceptor):
    """全 Worker に付ける（``Worker(..., interceptors=[ActivityLoggingInterceptor()])``）。"""

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LoggingActivityInbound(super().intercept_activity(next))


__all__ = ["ACTIVITY_INPUT_FIELDS", "ActivityLoggingInterceptor", "input_fields"]
