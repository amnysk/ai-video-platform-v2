"""Agent worker の Activity ``run_agent_stage``（ADR-0043 §6 Agent stage）。

順序（この順を変えない）:

1. pin された ``(role, spec_version, manifest_hash)`` で spec を解決。hash 不一致は**非再試行**の
   ``agent_spec_drift``（INV-51）
2. Feature flag（``jpmas2_agentic_workflows`` と ``enabled_agent_roles``）。無効は黙って飛ばさず
   ``blocked / agent_disabled``
3. 同じ冪等キーの結果が保存済みなら**何も書かず**それを返す（提案の再送だけは冪等に行う）
4. 入力 document の組み立て（正本のみ）。必須入力が無ければ ``blocked / input_missing``
5. 予算 gate と予約台帳（``reserve_and_dispatch_agent_call``）。
   拒否は ``blocked / <RejectReason>``、
   停止中は ``blocked / held``
6. Tool Gateway（LLM は Codex か Fake）を作って handler を呼ぶ
7. LLM を呼んだ事実を**検証より前**に台帳へ確定（``settle_agent_call``）
8. 成果物・agent_run・decision・revision_links・所見を同じ transaction で保存
9. 提案を ``ProposeStoryboardChange`` + 内部 port で出す（**適用はしない**。agent の scope は
   read / comment / propose だけ）

``AgentOutputRejected`` は ``agent_runs.outcome=rejected_output`` と結果 ``rejected_output``
（成果物なし）。再試行しても課金済みの LLM 呼び出しは繰り返さない（保存済みの応答を再生する）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from application.ports import Caller, CommandPort
from application.use_cases.propose_storyboard_change import ProposeStoryboardChange
from contracts.agentic import PlannedStage
from contracts.agentic_pipeline import (
    AGENT_SPEC_DRIFT_ERROR,
    RUN_AGENT_STAGE_ACTIVITY,
    AgentStageOutcome,
    RunAgentStageRequest,
    RunAgentStageResult,
    agent_stage_idempotency_key,
)
from contracts.agents import (
    AgentHandlerRef,
    AgentSpec,
    AgentTool,
    ModelPolicy,
    UnknownAgentSpecError,
    UnknownModelPolicyError,
    resolve_agent_spec,
    resolve_model_policy,
)
from contracts.application import (
    OperationError,
    ProposeStoryboardChangeRequest,
)
from contracts.intervention import ActorType, AgentRole, Scope
from contracts.states import ReservationStatus
from domain.agents.handlers import HANDLERS
from domain.agents.ports import AgentLlm
from domain.agents.runtime import AgentHandler, AgentInput, AgentOutput, AgentOutputRejected
from domain.agents.tool_gateway import ToolFn, ToolGateway
from domain.errors import (
    AgentBudgetRefusedError,
    EpisodeHeldError,
    ProviderInvocationError,
    UnreconciledReservationError,
)
from infrastructure.agents.budget_gate import (
    agent_reservation_key,
    reserve_and_dispatch_agent_call,
    settle_agent_call,
)
from infrastructure.agents.inputs import (
    AssembledInputs,
    PublicMetricsPort,
    assemble_agent_inputs,
    read_analytics_summary,
    read_current_artifact,
    required_inputs,
)
from infrastructure.agents.persist import (
    StageCapture,
    StageRecord,
    check_output_contract,
    load_stage_record,
    persist_stage,
)
from infrastructure.db.repositories import ProviderReservationRepository
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.capture import AGENT_RUNS_PREFIX, agent_run_id_for
from infrastructure.production.activity_errors import raise_activity_error
from infrastructure.storage.artifact_store import ArtifactStore
from workers.agents.llm import RecordingLlm, ReplayLlm

logger = logging.getLogger(__name__)

#: agent に与える scope。**apply は与えない**（提案だけ。INV-57 / ADR-0043 §1）
AGENT_SCOPES: frozenset[Scope] = frozenset({Scope.READ, Scope.COMMENT, Scope.PROPOSE})

REASON_AGENT_DISABLED = "agent_disabled"
REASON_HELD = "held"
REASON_INPUT_MISSING = "input_missing"

LlmFactory = Callable[[str, AgentSpec, ModelPolicy], AgentLlm]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _blocked(reason: str) -> RunAgentStageResult:
    return RunAgentStageResult(outcome=AgentStageOutcome.BLOCKED.value, reason=reason)


def agent_caller(role: AgentRole) -> Caller:
    return Caller(ActorType.AGENT, role.value, AGENT_SCOPES)


#: 提案の要求 command_id の namespace。変えると replay の冪等性が切れる
PROPOSAL_REQUEST_NAMESPACE = uuid.UUID("5c0e8d6a-7b31-5f42-8a9d-1e2b3c4d5e6f")


def proposal_request_id(idempotency_key: str, index: int) -> str:
    """提案ごとの要求 command_id。冪等キーから決まる（再送は同じ command 群になる）。"""
    return str(uuid.uuid5(PROPOSAL_REQUEST_NAMESPACE, f"{idempotency_key}:{index}"))


class AgentActivities:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        agentic_enabled: bool,
        enabled_roles: frozenset[AgentRole],
        budget_policy: BudgetPolicy,
        llm_factory: LlmFactory,
        command_port: CommandPort,
        metrics: PublicMetricsPort | None = None,
        handlers: Mapping[AgentHandlerRef, AgentHandler] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sf = session_factory
        self._store = store
        self._bucket = bucket
        self._agentic_enabled = agentic_enabled
        self._enabled_roles = enabled_roles
        self._budget_policy = budget_policy
        self._llm_factory = llm_factory
        self._port = command_port
        self._metrics = metrics
        self._handlers = HANDLERS if handlers is None else handlers
        self._clock = clock

    def activities(self) -> Sequence[Callable[..., object]]:
        return [self.run_agent_stage]

    # --- 入口 ---------------------------------------------------------------

    @activity.defn(name=RUN_AGENT_STAGE_ACTIVITY)
    async def run_agent_stage(self, request: RunAgentStageRequest) -> RunAgentStageResult:
        try:
            return await self._run(request)
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - ドメイン例外を型名つき ApplicationError にする
            raise_activity_error(exc)

    # --- 本体 ---------------------------------------------------------------

    def _resolve(
        self, request: RunAgentStageRequest
    ) -> tuple[PlannedStage, AgentSpec, ModelPolicy]:
        stage = PlannedStage.model_validate(request.stage)
        if stage.kind != "agent" or stage.agent_role is None or stage.agent_spec_version is None:
            raise ApplicationError(
                f"stage {stage.stage_id!r} is not an agent stage",
                type="invalid_agent_stage",
                non_retryable=True,
            )
        try:
            spec = resolve_agent_spec(stage.agent_role, stage.agent_spec_version)
            policy = resolve_model_policy(spec.model_policy_ref)
        except (UnknownAgentSpecError, UnknownModelPolicyError) as exc:
            raise ApplicationError(
                f"pinned agent spec is unknown: {exc}",
                type=AGENT_SPEC_DRIFT_ERROR,
                non_retryable=True,
            ) from None
        if (
            spec.manifest_hash != stage.agent_spec_hash
            or spec.handler_ref.value != stage.handler
            or spec.model_policy_ref != stage.model_policy_ref
            or spec.prompt_version != stage.prompt_version
        ):
            raise ApplicationError(
                f"agent spec {spec.role.value}@{spec.spec_version} no longer matches the plan "
                "(manifest_hash drift); the pinned definition is not run",
                type=AGENT_SPEC_DRIFT_ERROR,
                non_retryable=True,
            )
        return stage, spec, policy

    async def _run(self, request: RunAgentStageRequest) -> RunAgentStageResult:
        stage, spec, policy = self._resolve(request)
        if not self._agentic_enabled or spec.role not in self._enabled_roles:
            return _blocked(REASON_AGENT_DISABLED)
        key = agent_stage_idempotency_key(
            request.episode_id, stage.stage_id, request.plan_hash, request.idempotency_scope
        )

        async with self._sf() as session:
            stored = await load_stage_record(session, self._store, key)
        if stored is not None:
            return await self._finish(request, spec, key, stored)

        handler = self._handlers.get(spec.handler_ref)
        if handler is None:
            raise ApplicationError(
                f"no handler is registered for {spec.handler_ref.value}",
                type="agent_handler_missing",
                non_retryable=True,
            )

        inputs = await assemble_agent_inputs(
            session_factory=self._sf,
            store=self._store,
            episode_id=request.episode_id,
            handler_ref=spec.handler_ref,
            metrics=self._metrics,
        )
        missing = [n for n in required_inputs(spec.handler_ref) if n in inputs.missing()]
        if missing:
            return _blocked(f"{REASON_INPUT_MISSING}:{','.join(missing)}")

        started = self._clock()
        reservation = await self._reserve(request.episode_id, key, spec, policy)
        if isinstance(reservation, RunAgentStageResult):
            return reservation

        llm, replay = await self._llm_for(
            request.episode_id, spec, policy, reservation.raw_output_key
        )
        recording = RecordingLlm(llm)
        queued: list[dict[str, Any]] = []
        gateway = ToolGateway(
            spec, policy, recording, self._tools(request.episode_id, spec, queued)
        )
        output: AgentOutput | None = None
        rejected: str | None = None
        try:
            output = await handler(
                AgentInput(
                    episode_id=request.episode_id,
                    stage_id=stage.stage_id,
                    spec=spec,
                    documents=inputs.documents,
                    input_revisions=inputs.input_revisions,
                ),
                gateway,
            )
            if output.artifact is not None:
                check_output_contract(spec, output)
        except AgentOutputRejected as exc:
            output, rejected = None, str(exc)[:500]
        except BaseException as exc:
            await self._settle(reservation.id, recording, replay, key, request.episode_id, exc)
            raise
        raw_key = await self._settle(reservation.id, recording, replay, key, request.episode_id)

        capture = StageCapture(
            episode_id=request.episode_id,
            stage_id=stage.stage_id,
            idempotency_key=key,
            spec=spec,
            output=output,
            queued_proposals=tuple(queued),
            rejected_reason=rejected,
            invocations=gateway.invocations(),
            token_usage=gateway.token_usage(),
            cost_usd=gateway.actual_cost_usd(),
            calls=tuple(recording.calls),
            input_revisions=inputs.input_revisions,
            extra_refs={**inputs.extra_refs},
            reservation_id=reservation.id,
            raw_output_key=raw_key,
            started_at=started,
            finished_at=self._clock(),
        )
        async with self._sf() as session:
            record = await persist_stage(session, self._store, bucket=self._bucket, capture=capture)
            await session.commit()
        return await self._finish(request, spec, key, record)

    # --- 予算 gate・LLM ------------------------------------------------------

    async def _reserve(
        self, episode_id: str, key: str, spec: AgentSpec, policy: ModelPolicy
    ) -> Any:
        """予約して dispatch する。拒否・停止は ``RunAgentStageResult``（blocked）で返す。"""
        async with self._sf() as session:
            prior = await ProviderReservationRepository(session).find_by_key(
                agent_reservation_key(key)
            )
        if (
            prior is not None
            and prior.status is ReservationStatus.RESERVED
            and prior.dispatched_at is not None
            and prior.raw_output_key is None
        ):
            # 前の試行が dispatch の後で落ちた。呼んだか分からないので再送しない（ADR-0013）
            raise UnreconciledReservationError(
                f"reservation {prior.id} was dispatched without evidence"
            )
        async with self._sf() as session:
            try:
                reservation = await reserve_and_dispatch_agent_call(
                    session,
                    episode_id=episode_id,
                    idempotency_key=key,
                    spec=spec,
                    policy=policy,
                    budget_policy=self._budget_policy,
                    now=self._clock(),
                )
            except AgentBudgetRefusedError as exc:
                await session.rollback()
                return _blocked(exc.reason)
            except EpisodeHeldError:
                await session.rollback()
                return _blocked(REASON_HELD)
        if (
            reservation.raw_output_key is None
            and reservation.status is not ReservationStatus.RESERVED
        ):
            raise ProviderInvocationError(
                f"agent LLM reservation {reservation.id} was already consumed without a response"
            )
        return reservation

    async def _llm_for(
        self, episode_id: str, spec: AgentSpec, policy: ModelPolicy, raw_output_key: str | None
    ) -> tuple[AgentLlm, bool]:
        if raw_output_key is not None:
            # 前の試行が呼び終えている: 保存済みの応答を再生する（LLM を呼び直さない）
            recorded = (await self._store.get_json(raw_output_key)).get("calls") or []
            return ReplayLlm(recorded), True
        return self._llm_factory(episode_id, spec, policy), False

    async def _settle(
        self,
        reservation_id: str,
        recording: RecordingLlm,
        replay: bool,
        key: str,
        episode_id: str,
        error: BaseException | None = None,
    ) -> str | None:
        """「呼んだ」事実を台帳に確定する（検証・保存より前）。再生のときは確定済みなので何もしない。"""
        if replay:
            return None
        failed_without_response = error is not None and not recording.calls and recording.attempted
        if failed_without_response:
            async with self._sf() as session:
                await settle_agent_call(session, reservation_id, error=error)
            return None
        run_id = agent_run_id_for(key)
        raw_key = f"{AGENT_RUNS_PREFIX}/{episode_id}/{run_id}/llm-calls.json"
        await self._store.put_json(raw_key, recording.raw_document())
        async with self._sf() as session:
            await settle_agent_call(session, reservation_id, raw_output_key=raw_key)
        return raw_key

    # --- tool ---------------------------------------------------------------

    def _tools(
        self, episode_id: str, spec: AgentSpec, queued: list[dict[str, Any]]
    ) -> dict[AgentTool, ToolFn]:
        async def artifact_read(*, artifact_type: str) -> dict[str, Any]:
            return await read_current_artifact(
                session_factory=self._sf,
                store=self._store,
                episode_id=episode_id,
                artifact_type=artifact_type,
            )

        async def analytics_read() -> dict[str, Any]:
            return await read_analytics_summary(
                session_factory=self._sf, store=self._store, episode_id=episode_id
            )

        async def intervention_propose(*, proposal: Mapping[str, Any]) -> dict[str, Any]:
            """提案を**積むだけ**。出すのは保存の後（適用はしない）。"""
            queued.append(dict(proposal))
            return {"queued": len(queued)}

        return {
            AgentTool.ARTIFACT_READ: artifact_read,
            AgentTool.ANALYTICS_READ: analytics_read,
            AgentTool.INTERVENTION_PROPOSE: intervention_propose,
        }

    # --- 提案・結果 -----------------------------------------------------------

    async def _finish(
        self, request: RunAgentStageRequest, spec: AgentSpec, key: str, record: StageRecord
    ) -> RunAgentStageResult:
        command_ids: list[str] = []
        reason = record.reason
        if record.proposals:
            use_case = ProposeStoryboardChange(self._port)
            caller = agent_caller(spec.role)
            for index, proposal in enumerate(record.proposals):
                try:
                    result = await use_case.execute(
                        caller,
                        uuid.UUID(request.episode_id),
                        ProposeStoryboardChangeRequest.model_validate(
                            {
                                "command_id": proposal_request_id(key, index),
                                "proposal": proposal,
                            }
                        ),
                    )
                except (OperationError, ValidationError) as exc:
                    # 成果物は保存済み。提案が通らなくても stage は失敗にしない（理由を残す）
                    code = exc.code.value if isinstance(exc, OperationError) else "invalid_payload"
                    logger.warning(
                        "agent proposal rejected (stage=%s, code=%s)",
                        request.stage.get("stage_id"),
                        code,
                    )
                    reason = f"proposal_rejected:{code}"
                    continue
                command_ids.extend(c.command_id for c in result.commands)
        return RunAgentStageResult(
            outcome=record.outcome,
            reason=reason,
            artifact_revision_id=record.artifact_revision_id,
            agent_run_id=record.agent_run_id,
            decision_id=record.decision_id,
            proposal_command_ids=command_ids,
            replayed=record.replayed,
        )


__all__ = [
    "AGENT_SCOPES",
    "AgentActivities",
    "AssembledInputs",
    "agent_caller",
    "proposal_request_id",
]
