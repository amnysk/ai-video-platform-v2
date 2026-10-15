"""InterventionCommandWorkflow / InterventionRedeliveryWorkflow（ADR-0041 §3、intervention.md §4）。

I/O をしない（Activity を名前で呼ぶだけ）。``infrastructure`` を import しない（INV-40）::

    InterventionCommandWorkflow（id intervention-{command_id}、入力 {"command_id": ...}）
        apply_intervention_command（Episode の制御行を lock・終端なら保存済み result を返す）
        → resume / checkpoint 解放なら子 EpisodePipelineWorkflow を start（ABANDON、
          id episode-{id}-pipeline。同じ id が走っていれば already_running）
        → record_intervention_command_followup（start の結果を result に追記）
    InterventionRedeliveryWorkflow（Schedule avp-intervention-redelivery）
        list_undelivered_intervention_commands（未配送・停滞した command。配送済みを記録）
        → 各 command の InterventionCommandWorkflow を start（既に走っていれば成功扱い）

pipeline の入力は apply Activity が返す（worker の設定の production 枠を載せる）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from contracts.agentic_pipeline import (
        AGENTIC_EPISODE_WORKFLOW,
        AGENTIC_RESUME_PATCH_ID,
        PIPELINE_KIND_KEY,
        PipelineKind,
        agentic_episode_workflow_id,
    )
    from contracts.intervention import (
        APPLY_INTERVENTION_COMMAND_ACTIVITY,
        CHECK_PIPELINE_START_ACTIVITY,
        INTERVENTION_COMMAND_WORKFLOW,
        INTERVENTION_REDELIVERY_WORKFLOW,
        LIST_UNDELIVERED_COMMANDS_ACTIVITY,
        MARK_COMMAND_DISPATCHED_ACTIVITY,
        RECORD_COMMAND_FOLLOWUP_ACTIVITY,
        PipelineStartOutcome,
        intervention_command_workflow_id,
    )
    from contracts.pipeline import EPISODE_PIPELINE_WORKFLOW, pipeline_workflow_id

ACTIVITY_START_TO_CLOSE = timedelta(minutes=2)
ACTIVITY_SCHEDULE_TO_CLOSE = timedelta(hours=1)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
    #: command 行が無い（配送の誤り）は再試行しても直らない
    non_retryable_error_types=["CommandNotFoundError"],
)


async def _activity(name: str, arg: dict[str, Any]) -> dict[str, Any]:
    return await workflow.execute_activity(
        name,
        arg,
        result_type=dict,
        start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
        schedule_to_close_timeout=ACTIVITY_SCHEDULE_TO_CLOSE,
        retry_policy=ACTIVITY_RETRY,
    )


@workflow.defn(name=INTERVENTION_COMMAND_WORKFLOW[0])
class InterventionCommandWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        command_id = str(request["command_id"])
        outcome = await _activity(APPLY_INTERVENTION_COMMAND_ACTIVITY, {"command_id": command_id})
        start = outcome.get("pipeline_input")
        if isinstance(start, dict):
            pipeline = await self._start_pipeline(
                command_id,
                start,
                str(outcome.get("pipeline_task_queue")),
                # 判定は Activity（I/O 側）。旧い履歴の結果には無いので legacy になる。
                # patched は、この分岐を持たない旧 code で記録された履歴の replay を守る
                agentic=outcome.get(PIPELINE_KIND_KEY) == PipelineKind.AGENTIC.value
                and workflow.patched(AGENTIC_RESUME_PATCH_ID),
            )
            followed = await _activity(
                RECORD_COMMAND_FOLLOWUP_ACTIVITY,
                {"command_id": command_id, "pipeline": pipeline},
            )
            # replayed は「apply が保存済み result を返したか」。follow-up の記録は再適用ではない
            followed["replayed"] = outcome.get("replayed", False)
            outcome = followed
        return outcome

    @staticmethod
    async def _start_pipeline(
        command_id: str,
        pipeline_input: dict[str, Any],
        task_queue: str,
        agentic: bool = False,
    ) -> dict[str, Any]:
        """結果は必ず ``outcome`` を持つ（follow-up が必ず記録する。取り残しを再配送が拾う）。

        ``agentic`` なら ``AgenticEpisodeWorkflow``（id agentic-episode-{id}）。同じ id が
        走っていれば Temporal が拒否する（already_running）。閉じた実行の id は再利用できる
        （既定の ALLOW_DUPLICATE）。
        """
        episode_id = str(pipeline_input["episode_id"])
        workflow_name = AGENTIC_EPISODE_WORKFLOW[0] if agentic else EPISODE_PIPELINE_WORKFLOW[0]
        workflow_id = (
            agentic_episode_workflow_id(episode_id) if agentic else pipeline_workflow_id(episode_id)
        )
        verdict = await _activity(CHECK_PIPELINE_START_ACTIVITY, {"command_id": command_id})
        if not verdict.get("proceed"):
            return {
                "outcome": PipelineStartOutcome.NOT_NEEDED.value,
                "workflow_id": workflow_id,
                "reason": verdict.get("reason"),
            }
        try:
            # 完了を待たない。止めても有料の工程・投稿を途中で殺さない（ABANDON）
            await workflow.start_child_workflow(
                workflow_name,
                pipeline_input,
                id=workflow_id,
                task_queue=task_queue,
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            return {
                "outcome": PipelineStartOutcome.ALREADY_RUNNING.value,
                "workflow_id": workflow_id,
            }
        except Exception as exc:  # noqa: BLE001 - 失敗は記録して再配送に任せる（型だけ残す）
            return {
                "outcome": PipelineStartOutcome.START_FAILED.value,
                "workflow_id": workflow_id,
                "error": type(exc).__name__,
            }
        return {
            "outcome": PipelineStartOutcome.STARTED.value,
            "workflow_id": workflow_id,
            "start_stage": pipeline_input.get("start_stage"),
            "control_gate": pipeline_input.get("control_gate"),
        }


@workflow.defn(name=INTERVENTION_REDELIVERY_WORKFLOW[0])
class InterventionRedeliveryWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        listed = await _activity(LIST_UNDELIVERED_COMMANDS_ACTIVITY, dict(request or {}))
        started: list[str] = []
        running: list[str] = []
        for command_id in listed.get("command_ids", []):
            try:
                await workflow.start_child_workflow(
                    INTERVENTION_COMMAND_WORKFLOW[0],
                    {"command_id": command_id},
                    id=intervention_command_workflow_id(command_id),
                    task_queue=str(listed.get("task_queue") or INTERVENTION_COMMAND_WORKFLOW[1]),
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )
                started.append(command_id)
            except WorkflowAlreadyStartedError:
                running.append(command_id)
            # start に成功した、または既に走っている（それが適用・follow-up をする）ときだけ配送済み
            await _activity(MARK_COMMAND_DISPATCHED_ACTIVITY, {"command_id": command_id})
        return {"started": started, "already_running": running}
