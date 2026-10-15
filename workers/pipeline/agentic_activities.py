"""agentic 実行の pipeline 側 Activity（ADR-0043 §6）。状態の読み書きだけ（INV-4）。

- ``pin_workflow_plan``: Template をコンパイルして ``workflow_plan`` Artifact に固定する。
  Episode に現行の計画が既にあれば、**Template が変わっていても**それを返す（INV-51）。
- ``adopt_strategy_candidate``: producer の決定論の採用（``RationaleKind.RULE``）。現行
  StrategyBrief の ``recommended_candidate_id`` を採用し、``decision_records(choose_topic)`` と
  Episode の topic を書く。**TopicPlan は作らない**（日次 Planner と衝突する / ADR-0043）。
- ``load_workflow_plan``: pin 済みの計画を読むだけ（Learning が使う）。

``PipelineActivities`` とは別の class: flag 有効時だけ worker に登録する（INV-55）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from contracts.agentic import StrategyBrief, StrategyCandidate, WorkflowPlan
from contracts.agentic_pipeline import (
    ADOPT_STRATEGY_CANDIDATE_ACTIVITY,
    LOAD_WORKFLOW_PLAN_ACTIVITY,
    PIN_WORKFLOW_PLAN_ACTIVITY,
    AdoptStrategyCandidateRequest,
    AdoptStrategyCandidateResult,
    LoadWorkflowPlanRequest,
    PinWorkflowPlanRequest,
    PinWorkflowPlanResult,
)
from contracts.agents import UnknownAgentSpecError, resolve_agent_spec
from contracts.intervention import (
    DecisionCandidate,
    DecisionRecordPayload,
    InterventionEvent,
    RationaleKind,
)
from contracts.states import ArtifactType
from domain.workflow_templates.compiler import (
    TemplateCompileError,
    compile_agentic_template,
    operation_code_for,
)
from infrastructure.agents.episode_artifacts import read_current_artifact, write_episode_artifact
from infrastructure.db.models import DecisionRecordRow
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.records import CHOOSE_TOPIC, PRODUCER_ACTOR
from infrastructure.intervention.store import SYSTEM_ACTOR, InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore

PIN_WORKFLOW_PLAN_DECISION = "pin_workflow_plan"
NO_PINNED_PLAN = "no_pinned_plan"
EPISODE_NOT_FOUND = "episode_not_found"

#: 採用した候補以外の説明。規則の事実だけを書き、モデルの理由を補完しない
_NOT_RECOMMENDED = (
    "not recommended by the strategy brief (the producer adopts recommended_candidate_id)"
)


def _refuse(code: str, message: str) -> ApplicationError:
    """再試行しても直らない拒否。``type`` に機械可読の code を載せる。"""
    return ApplicationError(message, type=code, non_retryable=True)


class AgenticPipelineActivities:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket

    def activities(self) -> Sequence[Callable[..., object]]:
        return [self.pin_workflow_plan, self.adopt_strategy_candidate, self.load_workflow_plan]

    # ------------------------------------------------------------------ pin / load

    @staticmethod
    def _result(plan: WorkflowPlan, *, existing: bool) -> PinWorkflowPlanResult:
        """pin した spec の retry 上限を履歴に残す（workflow は Registry を読まない）。"""
        retries: dict[str, int] = {}
        for stage in (*plan.stages, *plan.post_publish):
            if stage.kind != "agent" or stage.agent_role is None or not stage.agent_spec_version:
                continue
            try:
                spec = resolve_agent_spec(stage.agent_role, stage.agent_spec_version)
            except UnknownAgentSpecError:
                continue  # 解決できない版は run_agent_stage が agent_spec_drift で止める
            retries[stage.stage_id] = max(1, spec.retry_max_attempts)
        return PinWorkflowPlanResult(
            plan=plan.model_dump(mode="json"), existing=existing, retry_max_attempts=retries
        )

    async def _pinned(self, session: AsyncSession, episode_id: str) -> WorkflowPlan | None:
        current = await read_current_artifact(
            session, self._store, episode_id, ArtifactType.WORKFLOW_PLAN
        )
        if current is None:
            return None
        plan = current[1]
        assert isinstance(plan, WorkflowPlan)
        return plan

    @activity.defn(name=PIN_WORKFLOW_PLAN_ACTIVITY)
    async def pin_workflow_plan(self, request: PinWorkflowPlanRequest) -> PinWorkflowPlanResult:
        async with self._session_factory() as session:
            if await EpisodeRepository(session).get(request.episode_id) is None:
                raise _refuse(EPISODE_NOT_FOUND, f"episode not found: {request.episode_id}")
            existing = await self._pinned(session, request.episode_id)
            if existing is not None:
                # 実行開始後に Template / Registry が変わっても計画は変えない（INV-51）
                return self._result(existing, existing=True)
            try:
                plan = compile_agentic_template(request.template_id, request.template_version)
            except TemplateCompileError as exc:
                raise _refuse(operation_code_for(exc).value, str(exc)) from exc
            revision_id = await write_episode_artifact(
                session,
                self._store,
                bucket=self._bucket,
                episode_id=request.episode_id,
                model=plan,
            )
            store = InterventionStore(session)
            payload = DecisionRecordPayload(
                decision_type=PIN_WORKFLOW_PLAN_DECISION,
                question=(
                    f"Which compiled plan runs episode {request.episode_id} "
                    f"({plan.template_id}@{plan.template_version})?"
                ),
                candidates=(
                    DecisionCandidate(
                        id=plan.plan_hash,
                        summary=(
                            f"{plan.template_id}@{plan.template_version}: "
                            + " -> ".join(s.stage_id for s in plan.stages)
                        )[:2000],
                    ),
                ),
                selected_candidate=plan.plan_hash,
                rationale_kind=RationaleKind.RULE,
                rationale="compiled deterministically from the registered template and agent specs",
                constraints=(
                    f"template_hash={plan.template_hash}",
                    *(
                        f"{s.stage_id}={s.agent_role.value}@{s.agent_spec_version}:{s.agent_spec_hash}"
                        for s in plan.stages
                        if s.agent_role is not None
                    ),
                ),
                result_revision=revision_id,
            )
            decision = await store.insert_decision(
                idempotency_key=f"decision:{PIN_WORKFLOW_PLAN_DECISION}:{request.episode_id}:{plan.plan_hash}",
                decision_type=PIN_WORKFLOW_PLAN_DECISION,
                actor=SYSTEM_ACTOR,
                payload=payload.model_dump(mode="json"),
                episode_id=request.episode_id,
                result_revision_id=revision_id,
            )
            await store.append_event(
                request.episode_id,
                InterventionEvent.DECISION_RECORDED,
                SYSTEM_ACTOR,
                {
                    "decision_id": decision.decision_id,
                    "decision_type": PIN_WORKFLOW_PLAN_DECISION,
                    "result_revision_id": revision_id,
                    "plan_hash": plan.plan_hash,
                    **decision.temporal.event_refs(),
                },
            )
            await session.commit()
        return self._result(plan, existing=False)

    @activity.defn(name=LOAD_WORKFLOW_PLAN_ACTIVITY)
    async def load_workflow_plan(self, request: LoadWorkflowPlanRequest) -> PinWorkflowPlanResult:
        async with self._session_factory() as session:
            plan = await self._pinned(session, request.episode_id)
        if plan is None:
            raise _refuse(NO_PINNED_PLAN, f"episode {request.episode_id} has no pinned plan")
        return self._result(plan, existing=True)

    # ------------------------------------------------------------------ producer 採用

    @staticmethod
    def _decision_payload(
        brief: StrategyBrief,
        chosen: StrategyCandidate,
        *,
        brief_revision_id: str,
        plan_hash: str,
    ) -> DecisionRecordPayload:
        candidates = tuple(
            DecisionCandidate(
                id=c.id,
                summary=f"{c.title} — {c.hook}"[:2000],
                benefits=(c.rationale,),
                concerns=c.risks,
                rejected_reason=(
                    None
                    if c.id == chosen.id
                    else (c.rejected_reason if c.status == "rejected" else _NOT_RECOMMENDED)
                ),
            )
            for c in brief.candidates
        )
        return DecisionRecordPayload(
            decision_type=CHOOSE_TOPIC,
            question="Which strategy candidate becomes this episode's topic?",
            candidates=candidates,
            selected_candidate=chosen.id,
            rationale_kind=RationaleKind.RULE,
            rationale=(
                "rule: the producer adopts the strategy brief's recommended_candidate_id "
                "when that candidate is proposed"
            ),
            constraints=(f"plan_hash={plan_hash}",),
            uncertainties=(
                f"audience basis is {brief.audience_basis}",
                *brief.uncertainties,
            ),
            target_revisions=(brief_revision_id,),
        )

    @activity.defn(name=ADOPT_STRATEGY_CANDIDATE_ACTIVITY)
    async def adopt_strategy_candidate(
        self, request: AdoptStrategyCandidateRequest
    ) -> AdoptStrategyCandidateResult:
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            if await episodes.get(request.episode_id) is None:
                raise _refuse(EPISODE_NOT_FOUND, f"episode not found: {request.episode_id}")
            current = await read_current_artifact(
                session, self._store, request.episode_id, ArtifactType.STRATEGY_BRIEF
            )
            if current is None:
                return AdoptStrategyCandidateResult(adopted=False, reason="no_strategy_brief")
            revision_id, brief = current
            assert isinstance(brief, StrategyBrief)
            if brief.episode_id != request.episode_id:
                return AdoptStrategyCandidateResult(
                    adopted=False, reason="strategy_brief_episode_mismatch"
                )
            chosen = next(
                (c for c in brief.candidates if c.id == brief.recommended_candidate_id), None
            )
            if chosen is None or chosen.status != "proposed":
                return AdoptStrategyCandidateResult(
                    adopted=False, reason="no_proposed_recommended_candidate"
                )
            store = InterventionStore(session)
            payload = self._decision_payload(
                brief, chosen, brief_revision_id=revision_id, plan_hash=request.plan_hash
            )
            key = f"decision:{CHOOSE_TOPIC}:agentic:{request.episode_id}:{request.plan_hash}"
            first_time = (
                await session.scalar(
                    select(DecisionRecordRow.id).where(DecisionRecordRow.idempotency_key == key)
                )
                is None
            )
            decision = await store.insert_decision(
                idempotency_key=key,
                decision_type=CHOOSE_TOPIC,
                actor=PRODUCER_ACTOR,
                payload=payload.model_dump(mode="json"),
                episode_id=request.episode_id,
            )
            if first_time:
                await store.append_event(
                    request.episode_id,
                    InterventionEvent.DECISION_RECORDED,
                    PRODUCER_ACTOR,
                    {
                        "decision_id": decision.decision_id,
                        "decision_type": CHOOSE_TOPIC,
                        "candidate_id": chosen.id,
                        **decision.temporal.event_refs(),
                    },
                )
            await episodes.set_topic(request.episode_id, chosen.title)
            await session.commit()
        return AdoptStrategyCandidateResult(
            adopted=True, candidate_id=chosen.id, decision_id=decision.decision_id
        )


__all__ = ["AgenticPipelineActivities"]
