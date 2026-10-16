"""JP-MAS-002 P1 の完了条件を、実 PostgreSQL + 実 Temporal + 実 MinIO で通す（P1-D）。

1 Episode が draft から strategy、producer 採用、creative、script、storyboard、
production、render、private upload、quality、(公開後) learning までを、本物の
``AgenticEpisodeWorkflow``・pipeline Activity（pin・採用・control gate・upload gate）・
``run_agent_stage``（本物の ``HANDLERS``）・永続化・``ProposeStoryboardChange`` で通る。
偽物は (1) LLM（``FakeAgentLlm`` + 定型応答。外に出ない）、(2) 既存工程の子 workflow
（script / storyboard / production / upload。render は fake engine）、
(3) 公開後の測定（``MockPublicMetrics``）だけ。有料 API・Codex・YouTube・fal には触れない。

安全: スキーマは使い捨て（``*_test`` DB の一時スキーマ）、Temporal は ``TEST_TEMPORAL_NAMESPACE``、
bucket は ``TEST_MINIO_BUCKET``。task queue ``agents`` と ``intervention`` は固定名なので、同じ
namespace で並列に走らせない。各テストの理由: docs/testing/jpmas2-p1d-rationale.md
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from application.use_cases.agentic_production import CreateEpisodeDraft
from contracts.agentic import UserBrief, WorkflowPlan
from contracts.agentic_pipeline import (
    AGENT_SPEC_DRIFT_ERROR,
    AGENTIC_EPISODE_WORKFLOW,
    AGENTIC_LEARNING_WORKFLOW,
    AGENTS_TASK_QUEUE,
    AgenticEpisodeInput,
    AgenticEpisodeResult,
    AgenticLearningInput,
    AgenticLearningResult,
    AgenticOutcome,
    AgentStageOutcome,
    PinWorkflowPlanRequest,
    RunAgentStageRequest,
    agent_stage_idempotency_key,
    agentic_episode_workflow_id,
    agentic_learning_workflow_id,
)
from contracts.agents import AGENT_SPECS, resolve_agent_spec
from contracts.application import CreateEpisodeDraftRequest
from contracts.intervention import (
    INTERVENTION_TASK_QUEUE,
    ActorType,
    AgentRole,
    AnnotationKind,
    ChangeSetStatus,
    CommandType,
    intervention_command_workflow_id,
)
from contracts.pipeline import EpisodePipelineInput, PipelineOptions
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from contracts.workflow_templates import TEMPLATES
from infrastructure.agents.budget_gate import agent_reservation_key
from infrastructure.agents.draft_port import DbEpisodeDraftPort
from infrastructure.agents.episode_artifacts import read_current_artifact
from infrastructure.agents.fake_llm import FAKE_MODEL, FakeAgentLlm
from infrastructure.config import Settings, enabled_agent_roles
from infrastructure.db.models import (
    AgentRunRow,
    AnnotationRow,
    ArtifactMetadataRow,
    Base,
    ChangeSetRow,
    DailyEpisodeSlotRow,
    DecisionRecordRow,
    EpisodeRow,
    InterventionCommandRow,
    ProviderReservationRow,
    TopicPlanRow,
)
from infrastructure.intervention.budget import BudgetPolicy, budget_policy_from_settings
from infrastructure.intervention.internal_port import SessionCommandPort
from tests.support.agentic_e2e import (
    FAKE_STAGE_WORKFLOWS,
    STRATEGY_TITLE,
    FakeStageActivities,
    MockPublicMetrics,
    canned,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.intervention import envelope, make_episode, receive, submit
from tests.support.temporal import connect_test_client, temporal_test_address
from workers.agents.activities import AgentActivities
from workers.agents.starter import TemporalCommandStarter
from workers.intervention.activities import InterventionActivities
from workers.intervention.workflows import (
    InterventionCommandWorkflow,
    InterventionRedeliveryWorkflow,
)
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.agentic_activities import AgenticPipelineActivities
from workers.pipeline.agentic_workflow import AgenticEpisodeWorkflow, AgenticLearningWorkflow
from workers.pipeline.run_worker import AGENTIC_WORKFLOWS, build_worker, registrations
from workers.pipeline.workflows import EpisodePipelineWorkflow

TEST_DATABASE_URL = require_test_database_url()
ADDRESS = temporal_test_address()
BUCKET = "artifacts"

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT") or not ADDRESS,
    reason="TEST_DATABASE_URL (*_test), MINIO_ENDPOINT and TEMPORAL_ADDRESS are required",
)

TEMPLATE_ORDER = [
    "strategy",
    "producer",
    "creative",
    "script",
    "storyboard",
    "production",
    "render",
    "private_upload",
    "quality",
]
STAGE_ROLE = {
    "strategy": AgentRole.STRATEGY,
    "creative": AgentRole.CREATIVE_DIRECTOR,
    "quality": AgentRole.QUALITY_REVIEWER,
}
NEW_ROLES = {r.value for r in (*STAGE_ROLE.values(), AgentRole.LEARNING)}
NEW_ARTIFACT_TYPES = {
    t.value
    for t in (
        ArtifactType.USER_BRIEF,
        ArtifactType.STRATEGY_BRIEF,
        ArtifactType.CREATIVE_BRIEF,
        ArtifactType.QUALITY_REVIEW,
        ArtifactType.LESSON,
        ArtifactType.WORKFLOW_PLAN,
    )
}


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"jpmas2_it_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from tests.support.minio import connect_test_artifact_store

    return await connect_test_artifact_store()


@pytest_asyncio.fixture
async def client() -> Client:
    return await connect_test_client()


@dataclass
class E2E:
    client: Client
    sf: Any
    store: Any
    queue: str
    stage_queue: str
    options: PipelineOptions
    settings: Settings
    llms: list[FakeAgentLlm] = field(default_factory=list)
    metrics: MockPublicMetrics = field(default_factory=MockPublicMetrics)
    stages: FakeStageActivities | None = None
    agent_acts: AgentActivities | None = None
    agentic: AgenticPipelineActivities | None = None

    # --- 実行 -----------------------------------------------------------------

    async def draft(self, theme: str = "How Edo firefighters fought fires") -> str:
        port = DbEpisodeDraftPort(session_factory=self.sf, store=self.store, bucket=BUCKET)
        brief = UserBrief(theme=theme, template_id="shorts_agentic", template_version="1")
        result = await CreateEpisodeDraft(port).execute(
            CreateEpisodeDraftRequest(command_id=f"draft-{uuid.uuid4().hex[:12]}", brief=brief)
        )
        assert result.created
        return str(result.episode_id)

    async def run(self, episode_id: str, *, suffix: str = "") -> AgenticEpisodeResult:
        return await self.client.execute_workflow(
            AGENTIC_EPISODE_WORKFLOW[0],
            AgenticEpisodeInput(
                episode_id=episode_id,
                template_id="shorts_agentic",
                template_version="1",
                options=self.options,
            ),
            id=agentic_episode_workflow_id(episode_id) + suffix,
            task_queue=self.queue,
            result_type=AgenticEpisodeResult,
        )

    async def learn(self, episode_id: str) -> AgenticLearningResult:
        return await self.client.execute_workflow(
            AGENTIC_LEARNING_WORKFLOW[0],
            AgenticLearningInput(episode_id=episode_id),
            id=agentic_learning_workflow_id(episode_id) + uuid.uuid4().hex[:6],
            task_queue=self.queue,
            result_type=AgenticLearningResult,
        )

    async def happy(self) -> tuple[str, AgenticEpisodeResult]:
        episode_id = await self.draft()
        result = await self.run(episode_id)
        assert result.outcome == AgenticOutcome.COMPLETED.value, result
        await self.wait_for_proposals(episode_id, 1)
        return episode_id, result

    async def wait_for_proposals(self, episode_id: str, expected: int) -> None:
        """提案 command は command workflow が非同期に ChangeSet にする。落ち着くまで待つ。"""
        ep = uuid.UUID(episode_id)
        for _ in range(150):
            if await self.count(ChangeSetRow, ChangeSetRow.episode_id == ep) >= expected:
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"{expected} proposed change set(s) did not appear")

    @property
    def llm_calls(self) -> int:
        return sum(len(llm.calls) for llm in self.llms)

    # --- DB の読み取り --------------------------------------------------------

    async def rows(self, model: Any, *where: Any) -> list[Any]:
        async with self.sf() as session:
            return list(await session.scalars(select(model).where(*where)))

    async def count(self, model: Any, *where: Any) -> int:
        async with self.sf() as session:
            return int(await session.scalar(select(func.count()).select_from(model).where(*where)))

    async def current(self, episode_id: str, artifact_type: ArtifactType) -> list[Any]:
        return await self.rows(
            ArtifactMetadataRow,
            ArtifactMetadataRow.episode_id == uuid.UUID(episode_id),
            ArtifactMetadataRow.artifact_type == artifact_type.value,
            ArtifactMetadataRow.superseded_at.is_(None),
        )

    async def ledger(self, episode_id: str) -> dict[str, int]:
        """二重実行の検査用: 書き込みの総数。"""
        ep = uuid.UUID(episode_id)
        return {
            "agent_runs": await self.count(AgentRunRow, AgentRunRow.episode_id == ep),
            "reservations": await self.count(
                ProviderReservationRow, ProviderReservationRow.episode_id == ep
            ),
            "artifacts": await self.count(
                ArtifactMetadataRow, ArtifactMetadataRow.episode_id == ep
            ),
            "change_sets": await self.count(ChangeSetRow, ChangeSetRow.episode_id == ep),
            "commands": await self.count(
                InterventionCommandRow, InterventionCommandRow.episode_id == ep
            ),
            "decisions": await self.count(DecisionRecordRow, DecisionRecordRow.episode_id == ep),
            "annotations": await self.count(AnnotationRow, AnnotationRow.episode_id == ep),
            "llm_calls": self.llm_calls,
        }

    async def agent_llm_reservations(self, episode_id: str) -> list[ProviderReservationRow]:
        return await self.rows(
            ProviderReservationRow,
            ProviderReservationRow.episode_id == uuid.UUID(episode_id),
            ProviderReservationRow.provider == ProviderCall.AGENT_LLM.value,
        )

    async def stage_request(self, episode_id: str, stage_id: str) -> RunAgentStageRequest:
        """pin 済みの計画から、workflow が送るのと同じ要求を作る。"""
        async with self.sf() as session:
            found = await read_current_artifact(
                session, self.store, episode_id, ArtifactType.WORKFLOW_PLAN
            )
        assert found is not None
        plan = found[1]
        assert isinstance(plan, WorkflowPlan)
        stage = next(s for s in (*plan.stages, *plan.post_publish) if s.stage_id == stage_id)
        return RunAgentStageRequest(
            episode_id=episode_id,
            stage=stage.model_dump(mode="json"),
            plan_hash=plan.plan_hash,
            template_id=plan.template_id,
            template_version=plan.template_version,
        )


@contextlib.asynccontextmanager
async def open_stack(
    client: Client,
    sf: Any,
    store: Any,
    tmp_path: Path,
    *,
    rate: Decimal | None = Decimal("150"),
) -> AsyncIterator[E2E]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        temporal_address=ADDRESS or "",
        temporal_namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "avp-test"),
        jpmas2_agentic_workflows=True,
        jpmas2_enabled_agents=",".join(sorted(NEW_ROLES)),
        jpmas2_agent_llm="fake",
        budget_usd_jpy_rate=rate,
    )
    queue = f"jpmas2-pipe-{uuid.uuid4().hex[:8]}"
    stage_queue = f"jpmas2-stages-{uuid.uuid4().hex[:8]}"
    options = PipelineOptions(
        script_workflow=("ScriptWorkflow", stage_queue),
        storyboard_workflow=("StoryboardWorkflow", stage_queue),
        production_workflow=("ProductionWorkflow", stage_queue),
        render_workflow=("RenderWorkflow", stage_queue),
        upload_workflow=("UploadWorkflow", stage_queue),
        pipeline_task_queue=queue,
    )
    e2e = E2E(client, sf, store, queue, stage_queue, options, settings)

    def llm_factory(_episode_id: str, _spec: Any, _policy: Any) -> FakeAgentLlm:
        llm = FakeAgentLlm(canned)
        e2e.llms.append(llm)
        return llm

    pipeline = PipelineActivities(session_factory=sf, paused_env=False, uploads_paused_env=False)
    e2e.agentic = AgenticPipelineActivities(session_factory=sf, store=store, bucket=BUCKET)
    e2e.stages = FakeStageActivities(sf, store, tmp_path)
    e2e.agent_acts = AgentActivities(
        session_factory=sf,
        store=store,
        bucket=BUCKET,
        agentic_enabled=settings.jpmas2_agentic_workflows,
        enabled_roles=enabled_agent_roles(settings),
        budget_policy=budget_policy_from_settings(settings),
        llm_factory=llm_factory,  # type: ignore[arg-type]
        command_port=SessionCommandPort(sf, TemporalCommandStarter(client, sf)),
        metrics=e2e.metrics,
    )
    intervention = InterventionActivities(
        session_factory=sf,
        store=store,
        budget_policy=BudgetPolicy(),
        pipeline_options=options,
        command_task_queue=INTERVENTION_TASK_QUEUE,
    )
    main = Worker(
        client,
        task_queue=queue,
        workflows=[AgenticEpisodeWorkflow, AgenticLearningWorkflow],
        activities=[*pipeline.activities(), *e2e.agentic.activities()],
    )
    stages = Worker(
        client,
        task_queue=stage_queue,
        workflows=FAKE_STAGE_WORKFLOWS,
        activities=[e2e.stages.run],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    agents = Worker(client, task_queue=AGENTS_TASK_QUEUE, activities=e2e.agent_acts.activities())
    commands = Worker(
        client,
        task_queue=INTERVENTION_TASK_QUEUE,
        workflows=[InterventionCommandWorkflow, InterventionRedeliveryWorkflow],
        activities=[*pipeline.activities(), *intervention.activities()],
    )
    async with main, stages, agents, commands:
        yield e2e


@pytest_asyncio.fixture
async def e2e(client, pg_session_factory, minio_store, tmp_path) -> AsyncIterator[E2E]:
    async with open_stack(client, pg_session_factory, minio_store, tmp_path) as stack:
        yield stack


# --------------------------------------------------------------------------- 1. happy path


async def test_one_episode_runs_every_stage_on_the_real_stack(e2e: E2E) -> None:
    """P1 完了条件: draft から private upload・品質所見まで 1 Episode が通り、証跡が全て残る。"""
    episode_id, result = await e2e.happy()
    ep = uuid.UUID(episode_id)

    assert result.completed_stages == TEMPLATE_ORDER
    assert result.stopped_stage is None and result.plan_hash
    assert [s for (_e, s) in e2e.stages.ran] == [  # type: ignore[union-attr]
        "ScriptWorkflow",
        "StoryboardWorkflow",
        "ProductionWorkflow",
        "RenderWorkflow",
        "UploadWorkflow",
    ]

    # 現行の Artifact は種類ごとに 1 つ
    for artifact_type in (
        ArtifactType.WORKFLOW_PLAN,
        ArtifactType.USER_BRIEF,
        ArtifactType.STRATEGY_BRIEF,
        ArtifactType.CREATIVE_BRIEF,
        ArtifactType.QUALITY_REVIEW,
    ):
        assert len(await e2e.current(episode_id, artifact_type)) == 1, artifact_type

    # 3 つの role が 1 回ずつ。model は fake、費用は測れない（NULL。0 にしない）
    assert FAKE_MODEL == "fake-deterministic"
    runs = await e2e.rows(
        AgentRunRow, AgentRunRow.episode_id == ep, AgentRunRow.role.in_(NEW_ROLES)
    )
    assert sorted(r.role for r in runs) == sorted(r.value for r in STAGE_ROLE.values())
    assert {r.model for r in runs} == {"fake-deterministic"}
    assert all(r.estimated_cost_usd is None for r in runs)
    assert all(r.token_usage is not None for r in runs)  # 使用量は測れた

    # 判断記録
    decisions = await e2e.rows(DecisionRecordRow, DecisionRecordRow.episode_id == ep)
    by_type = {d.decision_type: d for d in decisions}
    assert {"strategy_brief", "choose_topic", "creative_brief", "quality_review"} <= set(by_type)
    choose = by_type["choose_topic"]
    assert (choose.actor_type, choose.actor_id) == (ActorType.AGENT.value, "producer")
    for kind, role in (
        ("strategy_brief", AgentRole.STRATEGY),
        ("creative_brief", AgentRole.CREATIVE_DIRECTOR),
        ("quality_review", AgentRole.QUALITY_REVIEWER),
    ):
        assert (by_type[kind].actor_type, by_type[kind].actor_id) == ("agent", role.value)

    # producer の採用: Episode の topic。日次の TopicPlan・枠は使わない
    async with e2e.sf() as session:
        episode = await session.get(EpisodeRow, ep)
    assert episode is not None and episode.topic == STRATEGY_TITLE
    assert await e2e.count(TopicPlanRow) == 0
    assert await e2e.count(DailyEpisodeSlotRow) == 0

    # QA の所見は注記、修正は提案だけ（適用しない）
    findings = await e2e.rows(
        AnnotationRow,
        AnnotationRow.episode_id == ep,
        AnnotationRow.kind == AnnotationKind.REVIEW_FINDING.value,
        AnnotationRow.actor_type == ActorType.AGENT.value,
        AnnotationRow.actor_id == AgentRole.QUALITY_REVIEWER.value,  # 規則 reviewer の所見は別
    )
    assert len(findings) == 2
    proposals = await e2e.rows(ChangeSetRow, ChangeSetRow.episode_id == ep)
    assert len(proposals) == 1 and result.qa_repair_cycles == 1
    assert proposals[0].status == ChangeSetStatus.PROPOSED.value
    assert proposals[0].actor_type == ActorType.AGENT.value
    assert proposals[0].actor_id == AgentRole.QUALITY_REVIEWER.value
    commands = await e2e.rows(
        InterventionCommandRow,
        InterventionCommandRow.episode_id == ep,
        InterventionCommandRow.type == CommandType.PROPOSE_CHANGE.value,
    )
    assert len(commands) == 1

    # 予約は上限で数える
    reservations = await e2e.agent_llm_reservations(episode_id)
    assert len(reservations) == 3
    ceilings = {
        agent_reservation_key(
            agent_stage_idempotency_key(episode_id, stage, result.plan_hash or "")
        ): resolve_agent_spec(role, "1.0").max_estimated_cost_usd
        for stage, role in STAGE_ROLE.items()
    }
    assert {r.idempotency_key: r.estimated_cost_usd for r in reservations} == ceilings


# --------------------------------------------------------------------------- 2. learning


async def test_learning_waits_for_public_metrics_then_proposes_one_lesson(e2e: E2E) -> None:
    """公開前は awaiting_public_metrics（Lesson なし）、公開後は proposed の Lesson が 1 つ。"""
    episode_id, _ = await e2e.happy()

    private = await e2e.learn(episode_id)
    assert private.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value
    assert private.lesson_revision_id is None
    assert await e2e.current(episode_id, ArtifactType.LESSON) == []
    assert e2e.metrics.calls == ["abcdefghijk"]  # 投稿受領の video_id で測定を問うた

    e2e.metrics.public = True
    public = await e2e.learn(episode_id)
    assert public.outcome == AgentStageOutcome.PRODUCED.value, public
    lessons = await e2e.current(episode_id, ArtifactType.LESSON)
    assert len(lessons) == 1 and str(lessons[0].id) == public.lesson_revision_id
    lesson = await e2e.store.get_json(lessons[0].object_key)
    assert lesson["status"] == "proposed"
    assert lesson["video_id"] == "abcdefghijk"
    assert [w["window"] for w in lesson["measurement_windows"]] == ["48h"]
    # 小標本は断定しない
    assert lesson["sample_note"] is not None


# --------------------------------------------------------------------------- 3. idempotency


async def test_a_replayed_run_adds_no_runs_reservations_artifacts_or_proposals(e2e: E2E) -> None:
    """再実行しても LLM 呼び出し・予約・成果物・提案は 1 件も増えない（二重課金なし）。"""
    episode_id, first = await e2e.happy()
    before = await e2e.ledger(episode_id)
    assert before["llm_calls"] == 3 and before["reservations"] == 3

    # (a) 同じ Episode の workflow を新しい id で再実行する。投稿済みなので upload gate が止める
    again = await e2e.run(episode_id, suffix="-again")
    assert again.plan_hash == first.plan_hash
    assert again.stopped_stage == "private_upload"
    assert again.completed_stages == TEMPLATE_ORDER[:7]
    assert await e2e.ledger(episode_id) == before

    # (b) 同じ冪等キーの Activity を直接再送する: 保存済みの結果を返し、何も書かない
    for stage_id in ("strategy", "creative", "quality"):
        request = await e2e.stage_request(episode_id, stage_id)
        replayed = await e2e.agent_acts.run_agent_stage(request)  # type: ignore[union-attr]
        assert replayed.outcome == AgentStageOutcome.PRODUCED.value
        assert replayed.replayed is True
        if stage_id == "quality":
            # 提案は同じ command 群として返る（ProposeStoryboardChange の冪等な再送）
            async with e2e.sf() as session:
                known = set(
                    await session.scalars(
                        select(InterventionCommandRow.id).where(
                            InterventionCommandRow.episode_id == uuid.UUID(episode_id),
                            InterventionCommandRow.type == CommandType.PROPOSE_CHANGE.value,
                        )
                    )
                )
            assert set(replayed.proposal_command_ids) == known and len(known) == 1
    assert await e2e.ledger(episode_id) == before


# --------------------------------------------------------------------------- 4. pinning


async def test_a_pinned_plan_survives_a_changed_template(e2e: E2E, monkeypatch) -> None:
    """pin した計画は、その後 Template が変わっても同じ plan_hash で使われる（INV-51）。"""
    episode_id = await e2e.draft()
    pinned = await e2e.agentic.pin_workflow_plan(  # type: ignore[union-attr]
        PinWorkflowPlanRequest(episode_id, "shorts_agentic", "1")
    )
    plan = WorkflowPlan.model_validate(pinned.plan)

    original = TEMPLATES[("shorts_agentic", "1")]
    changed = dataclasses.replace(original, stages=original.stages[:-1])  # quality を落とす
    monkeypatch.setitem(TEMPLATES, ("shorts_agentic", "1"), changed)

    result = await e2e.run(episode_id)
    assert result.outcome == AgenticOutcome.COMPLETED.value, result
    assert result.plan_hash == plan.plan_hash
    assert result.completed_stages == TEMPLATE_ORDER  # 変わった定義ではなく pin 済みの計画
    assert len(await e2e.current(episode_id, ArtifactType.WORKFLOW_PLAN)) == 1
    assert len(await e2e.current(episode_id, ArtifactType.QUALITY_REVIEW)) == 1


async def test_a_drifted_agent_spec_stops_with_one_attempt(e2e: E2E, monkeypatch) -> None:
    """pin 後に spec の hash が変わると agent_spec_drift で止まる。試行は 1 回（非再試行）。"""
    import workers.agents.activities as activities

    episode_id = await e2e.draft()
    await e2e.agentic.pin_workflow_plan(  # type: ignore[union-attr]
        PinWorkflowPlanRequest(episode_id, "shorts_agentic", "1")
    )
    key = (AgentRole.STRATEGY, "1.0")
    drifted = dataclasses.replace(AGENT_SPECS[key], max_tokens=AGENT_SPECS[key].max_tokens - 1)
    assert drifted.manifest_hash != AGENT_SPECS[key].manifest_hash
    monkeypatch.setitem(AGENT_SPECS, key, drifted)
    attempts: list[str] = []
    real = activities.resolve_agent_spec

    def counting(*args: Any, **kwargs: Any) -> Any:
        attempts.append(str(args[0]))
        return real(*args, **kwargs)

    monkeypatch.setattr(activities, "resolve_agent_spec", counting)

    result = await e2e.run(episode_id)
    assert result.outcome == AgenticOutcome.STOPPED.value
    assert result.stopped_stage == "strategy"
    assert AGENT_SPEC_DRIFT_ERROR in (result.reason or "")
    assert len(attempts) == 1  # retry_max_attempts は 3 だが、drift は再試行しない
    assert e2e.llm_calls == 0
    assert await e2e.agent_llm_reservations(episode_id) == []
    assert await e2e.current(episode_id, ArtifactType.STRATEGY_BRIEF) == []


# --------------------------------------------------------------------------- 5. hold


async def test_a_hold_requested_before_start_parks_at_the_first_stage(e2e: E2E) -> None:
    """停止中の Episode は、最初の agent stage を起動せず parked で止まる（予約・LLM なし）。"""
    episode_id = await e2e.draft()
    outcome = await submit(e2e.sf, e2e.store, episode_id, envelope(CommandType.REQUEST_HOLD))
    assert outcome.status.value == "applied"

    result = await e2e.run(episode_id)
    assert result.outcome == AgenticOutcome.PARKED.value, result
    assert result.stopped_stage == "strategy" and result.completed_stages == []
    assert e2e.llm_calls == 0
    assert await e2e.agent_llm_reservations(episode_id) == []
    assert await e2e.current(episode_id, ArtifactType.STRATEGY_BRIEF) == []
    assert e2e.stages.ran == []  # type: ignore[union-attr]


async def test_a_hold_then_resume_restarts_the_agentic_workflow_without_double_work(
    e2e: E2E,
) -> None:
    """介入 command の resume が `AgenticEpisodeWorkflow` を再起動する（ADR-0043 の負債の解消）。

    停止中に最初の起動が parked で閉じ、resume が同じ id で再起動して全工程を通る。
    完了後にもう一度 hold -> resume しても再開する工程が無いので起動せず、
    LLM・予約・投稿は増えない。
    理由: docs/testing/jpmas2-fixa-rationale.md
    """
    episode_id = await e2e.draft()
    await submit(e2e.sf, e2e.store, episode_id, envelope(CommandType.REQUEST_HOLD))
    parked = await e2e.run(episode_id)
    assert parked.outcome == AgenticOutcome.PARKED.value and e2e.llm_calls == 0

    async def resume() -> dict[str, Any]:
        command = envelope(CommandType.RESUME)
        await receive(e2e.sf, episode_id, command)
        return await e2e.client.execute_workflow(
            "InterventionCommandWorkflow",
            {"command_id": command.command_id},
            id=intervention_command_workflow_id(command.command_id),
            task_queue=INTERVENTION_TASK_QUEUE,
            result_type=dict,
        )

    first = await resume()
    assert first["pipeline"]["outcome"] == "started"
    assert first["pipeline"]["workflow_id"] == agentic_episode_workflow_id(episode_id)
    handle = e2e.client.get_workflow_handle(agentic_episode_workflow_id(episode_id))
    finished = await handle.result()  # AgenticEpisodeResult の dict
    assert finished["outcome"] == AgenticOutcome.COMPLETED.value, finished
    assert finished["plan_hash"] == parked.plan_hash
    await e2e.wait_for_proposals(episode_id, 1)
    before = await e2e.ledger(episode_id)
    assert before["llm_calls"] == 3 and before["reservations"] == 3
    uploads = [s for (_e, s) in e2e.stages.ran].count("UploadWorkflow")  # type: ignore[union-attr]

    await submit(e2e.sf, e2e.store, episode_id, envelope(CommandType.REQUEST_HOLD))
    second = await resume()
    # 完了・投稿済みの Episode は再開する工程が無い（domain の resume plan が
    # pipeline_start を返さない。旧経路と同じ）。workflow は起動されない
    assert second["status"] == "applied" and second["pipeline"] is None, second
    after = await e2e.ledger(episode_id)
    assert after.pop("commands") == before.pop("commands") + 2  # 送った hold と resume の2件だけ
    assert after == before  # LLM・予約・agent run・成果物は増えない
    ran_uploads = [s for (_e, s) in e2e.stages.ran].count("UploadWorkflow")  # type: ignore[union-attr]
    assert ran_uploads == uploads == 1  # 投稿は二重に走らない


# --------------------------------------------------------------------------- 6. budget


async def test_a_missing_exchange_rate_blocks_the_first_agent_stage(
    client, pg_session_factory, minio_store, tmp_path
) -> None:
    """BUDGET_USD_JPY_RATE が無いと予算を検証できない: LLM を呼ばず、予約も成果物も作らない。"""
    async with open_stack(client, pg_session_factory, minio_store, tmp_path, rate=None) as e2e:
        assert e2e.settings.budget_usd_jpy_rate is None
        episode_id = await e2e.draft()
        result = await e2e.run(episode_id)
        assert result.outcome == AgenticOutcome.STOPPED.value
        assert result.stopped_stage == "strategy"
        assert result.reason == "agent stage blocked: budget_unverifiable"
        assert e2e.llm_calls == 0
        assert await e2e.agent_llm_reservations(episode_id) == []
        assert await e2e.current(episode_id, ArtifactType.STRATEGY_BRIEF) == []
        assert await e2e.count(AgentRunRow, AgentRunRow.episode_id == uuid.UUID(episode_id)) == 0


# --------------------------------------------------------------------------- 7. daily isolation


async def test_the_default_pipeline_worker_is_unchanged_and_a_legacy_run_writes_nothing_agentic(
    e2e: E2E,
) -> None:
    """flag 既定のままの pipeline worker は agentic を登録せず、旧 pipeline の実行は従来どおり。"""
    default_settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert default_settings.jpmas2_agentic_workflows is False
    pipeline = PipelineActivities(
        session_factory=e2e.sf, paused_env=False, uploads_paused_env=False
    )
    workflows, acts = registrations(pipeline)
    assert not set(AGENTIC_WORKFLOWS) & set(workflows)
    assert not {"pin_workflow_plan", "adopt_strategy_candidate", "load_workflow_plan"} & {
        getattr(a, "__temporal_activity_definition").name for a in acts
    }

    legacy_queue = f"jpmas2-legacy-{uuid.uuid4().hex[:8]}"
    async with build_worker(e2e.client, pipeline, task_queue=legacy_queue):
        async with e2e.sf() as session:
            episode_id = await make_episode(session)
        options = dataclasses.replace(e2e.options, pipeline_task_queue=legacy_queue)
        result = await e2e.client.execute_workflow(
            EpisodePipelineWorkflow.run,
            EpisodePipelineInput(episode_id=episode_id, options=options),
            id=f"legacy-{episode_id}",
            task_queue=legacy_queue,
        )
    assert result.outcome == "completed", result
    assert result.completed_stages == ["script", "storyboard", "production", "render", "upload"]
    async with e2e.sf() as session:
        episode = await session.get(EpisodeRow, uuid.UUID(episode_id))
    assert episode is not None and episode.status == EpisodeStatus.UPLOADED.value
    ep = uuid.UUID(episode_id)
    assert (
        await e2e.count(AgentRunRow, AgentRunRow.episode_id == ep, AgentRunRow.role.in_(NEW_ROLES))
        == 0
    )
    assert (
        await e2e.count(
            ArtifactMetadataRow,
            ArtifactMetadataRow.episode_id == ep,
            ArtifactMetadataRow.artifact_type.in_(NEW_ARTIFACT_TYPES),
        )
        == 0
    )
    assert await e2e.agent_llm_reservations(episode_id) == []
    assert e2e.llm_calls == 0
