"""JP-MAS-002 P2-A の Application API を、HTTP 入口から実 PostgreSQL・Temporal・MinIO で通す。

``apps.api.main.create_app`` の FastAPI app を ASGITransport で叩く。**本物**: route・認証
（principal registry + Bearer）・``get_application_service`` の組み立て（``ApplicationService``・
``StoreBackedEpisodeReader``・``DbEpisodeDraftPort``）・``StoreBackedInterventionBackend``・
``LazyAgenticStarter``・実 Temporal の command workflow と ``AgenticEpisodeWorkflow``。
**差し替えたのは配線だけ**: session factory・artifact store・Temporal 接続・settings（隔離資源へ）。
偽物は P1-D の E2E と同じ (1) LLM（``FakeAgentLlm``）、(2) 既存工程の子 workflow（fake）だけ。
有料 API・Codex・YouTube・fal には触れない。

安全: スキーマは使い捨て（``*_test`` DB）、Temporal は ``TEST_TEMPORAL_NAMESPACE``、bucket は
``TEST_MINIO_BUCKET``。task queue ``agents`` / ``intervention`` は固定名なので、同じ namespace で
並列に走らせない。各テストの理由: docs/testing/jpmas2-p2a-e2e-rationale.md
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from apps.api.auth import token_sha256
from contracts.agentic import UserBrief
from contracts.agentic_pipeline import AgenticOutcome, agentic_episode_workflow_id
from contracts.application import WorkerCheck
from contracts.application_operations import (
    ChangeImpactPreview,
    EpisodeList,
    EpisodeWorkspace,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    ResumeAccepted,
    WorkflowProgress,
)
from contracts.intervention import CommandType
from contracts.pipeline import pipeline_workflow_id
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from infrastructure.config import Settings
from infrastructure.db.models import (
    AgentRunRow,
    ArtifactMetadataRow,
    Base,
    ChangeSetRow,
    InterventionCommandRow,
    ProviderReservationRow,
)
from tests.integration.test_jpmas2_agentic_e2e import (
    ADDRESS,
    E2E,
    NEW_ROLES,
    open_stack,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.intervention import make_episode, seed_script_and_storyboard
from tests.support.temporal import connect_test_client

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT") or not ADDRESS,
    reason="TEST_DATABASE_URL (*_test), MINIO_ENDPOINT and TEMPORAL_ADDRESS are required",
)

PREFIX = "/v1/intervention"
HUMAN = "p2a-e2e-human-token-0001"
AI = "p2a-e2e-ai-token-000000002"
ALL_SCOPES = ["read", "comment", "propose", "hold", "apply", "resume", "set_mode"]
AI_SCOPES = ["read", "comment", "propose", "hold", "apply", "resume"]


def _principals() -> str:
    return json.dumps(
        {
            token_sha256(HUMAN): {"actor_type": "human", "actor_id": "owner", "scopes": ALL_SCOPES},
            token_sha256(AI): {
                "actor_type": "ai_assistant",
                "actor_id": "chat-assistant",
                "scopes": AI_SCOPES,
            },
        }
    )


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"p2ahttp_it_{uuid.uuid4().hex[:12]}"
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
class Api:
    e2e: E2E
    http: AsyncClient
    #: HTTP が見る settings。flag を切り替えるテストは ``set_flags`` で差し替える
    settings: Settings

    async def call(
        self, method: str, path: str, *, token: str = HUMAN, body: dict[str, Any] | None = None
    ) -> Any:
        return await self.http.request(
            method, f"{PREFIX}{path}", headers={"Authorization": f"Bearer {token}"}, json=body
        )

    def set_flags(self, **update: Any) -> None:
        self.settings = self.settings.model_copy(update=update)

    # --- 操作の短縮 -------------------------------------------------------------

    async def draft(self, theme: str = "How Edo firefighters fought fires") -> str:
        response = await self.call("POST", "/episodes/drafts", body=_draft_body(theme))
        assert response.status_code == 201, response.text
        return response.json()["episode_id"]

    async def produce(self) -> str:
        """人が draft を作って起動し、workflow が完了するまで待つ（P1-D の E2E の到達点まで）。"""
        episode_id = await self.draft()
        response = await self.call("POST", f"/episodes/{episode_id}/production")
        assert response.status_code == 202, response.text
        finished = await self.e2e.client.get_workflow_handle(
            agentic_episode_workflow_id(episode_id)
        ).result()
        assert finished["outcome"] == AgenticOutcome.COMPLETED.value, finished
        await self.e2e.wait_for_proposals(episode_id, 1)
        return episode_id

    async def wait_command(self, command_id: str, want: str = "applied") -> dict[str, Any]:
        for _ in range(150):
            response = await self.call("GET", f"/commands/{command_id}")
            if response.status_code == 200 and response.json()["status"] == want:
                return response.json()
            await asyncio.sleep(0.2)
        raise AssertionError(f"command {command_id} did not become {want}")

    # --- 台帳（二重実行の検査） ---------------------------------------------------

    async def counts(self, episode_id: str) -> dict[str, int]:
        ep = uuid.UUID(episode_id)
        e2e = self.e2e
        return {
            "agent_runs": await e2e.count(AgentRunRow, AgentRunRow.episode_id == ep),
            "reservations": await e2e.count(
                ProviderReservationRow, ProviderReservationRow.episode_id == ep
            ),
            "commands": await e2e.count(
                InterventionCommandRow, InterventionCommandRow.episode_id == ep
            ),
            "change_sets": await e2e.count(ChangeSetRow, ChangeSetRow.episode_id == ep),
        }

    async def command_count(self, episode_id: str, command_type: CommandType) -> int:
        return await self.e2e.count(
            InterventionCommandRow,
            InterventionCommandRow.episode_id == uuid.UUID(episode_id),
            InterventionCommandRow.type == command_type.value,
        )

    async def workflow_exists(self, episode_id: str) -> bool:
        handle = self.e2e.client.get_workflow_handle(agentic_episode_workflow_id(episode_id))
        try:
            await handle.describe()
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return False
            raise
        return True


def _draft_body(theme: str, command_id: str | None = None) -> dict[str, Any]:
    brief = UserBrief(theme=theme, template_id="shorts_agentic", template_version="1")
    return {
        "command_id": command_id or f"draft-{uuid.uuid4().hex[:12]}",
        "brief": brief.model_dump(mode="json"),
    }


@pytest_asyncio.fixture
async def api(client, pg_session_factory, minio_store, tmp_path, monkeypatch) -> AsyncIterator[Api]:
    from apps.api import application_service
    from apps.api.dependencies import get_settings
    from apps.api.intervention_backend import StoreBackedInterventionBackend, load_real_modules
    from apps.api.main import create_app
    from apps.api.routers import intervention
    from infrastructure.temporal import pipeline_options
    from tests.support.minio import require_test_minio_bucket

    async with open_stack(client, pg_session_factory, minio_store, tmp_path) as e2e:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            temporal_address=ADDRESS or "",
            temporal_namespace=e2e.settings.temporal_namespace,
            minio_bucket=require_test_minio_bucket(),
            intervention_principals=_principals(),  # type: ignore[arg-type]
            jpmas2_application_api=True,
            jpmas2_agentic_workflows=True,
            jpmas2_enabled_agents=e2e.settings.jpmas2_enabled_agents,
            jpmas2_agent_llm="fake",
            budget_usd_jpy_rate=e2e.settings.budget_usd_jpy_rate,
        )
        # 配線だけを隔離資源へ向ける（service・backend・starter の論理は本物）
        monkeypatch.setattr(application_service, "get_session_factory", lambda: e2e.sf)
        monkeypatch.setattr(
            application_service, "get_artifact_store_factory", lambda: lambda: e2e.store
        )

        async def connect(_: Settings) -> Client:
            return e2e.client

        monkeypatch.setattr(application_service, "_connect_temporal", connect)
        monkeypatch.setattr(
            pipeline_options, "pipeline_options_from_settings", lambda _s: e2e.options
        )

        backend = StoreBackedInterventionBackend(e2e.sf, load_real_modules(settings, e2e.sf))
        app = create_app()
        harness = Api(e2e, None, settings)  # type: ignore[arg-type]
        app.dependency_overrides[get_settings] = lambda: harness.settings
        app.dependency_overrides[intervention.get_intervention_backend] = lambda: backend
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            harness.http = http
            yield harness


def _detail(response: Any) -> dict[str, Any]:
    return response.json()["detail"]


# --------------------------------------------------------------------------- 1. draft


async def test_a_human_creates_a_draft_and_resending_it_returns_the_same_episode(api: Api) -> None:
    body = _draft_body("Why castles had crooked roads")
    first = await api.call("POST", "/episodes/drafts", body=body)
    assert first.status_code == 201, first.text
    episode_id = first.json()["episode_id"]
    assert first.json()["created"] is True

    again = await api.call("POST", "/episodes/drafts", body=body)
    assert again.status_code == 200, again.text
    assert again.json()["episode_id"] == episode_id and again.json()["created"] is False
    assert await api.e2e.count(AgentRunRow) == 0  # draft は何も走らせない

    listed = EpisodeList.model_validate((await api.call("GET", "/episodes")).json())
    assert [str(e.episode_id) for e in listed.episodes].count(episode_id) == 1

    response = await api.call("GET", f"/episodes/{episode_id}/workspace")
    assert response.status_code == 200, response.text
    workspace = EpisodeWorkspace.model_validate(response.json())
    assert str(workspace.episode.episode_id) == episode_id
    # pin 前の draft は旧 pipeline の扱い（pipeline_kind は pin 済みの計画で決まる）
    assert workspace.episode.pipeline_kind.value == "legacy"
    assert workspace.workflow_id == pipeline_workflow_id(episode_id)
    assert workspace.pending_change_sets == ()

    # 同じ command_id で別の内容は再利用扱い（部分的に別 Episode を作らない）
    reused = await api.call(
        "POST", "/episodes/drafts", body=_draft_body("A different theme", body["command_id"])
    )
    assert reused.status_code == 409 and _detail(reused)["error"] == "command_id_reused"


# --------------------------------------------------------------------------- 2. start production


async def test_start_production_runs_the_workflow_once_and_a_second_post_adds_nothing(
    api: Api,
) -> None:
    episode_id = await api.draft()
    first = await api.call("POST", f"/episodes/{episode_id}/production")
    assert first.status_code == 202, first.text
    started = first.json()
    assert started["started"] is True
    assert started["workflow_id"] == agentic_episode_workflow_id(episode_id)
    assert started["worker_check"] == WorkerCheck.POLLERS_PRESENT.value  # 隔離 stack の worker

    finished = await api.e2e.client.get_workflow_handle(started["workflow_id"]).result()
    assert finished["outcome"] == AgenticOutcome.COMPLETED.value, (
        finished
    )  # P1-D の E2E と同じ到達点
    await api.e2e.wait_for_proposals(episode_id, 1)
    before = await api.counts(episode_id)
    assert before["reservations"] == 3 and before["agent_runs"] >= 3
    assert api.e2e.llm_calls == 3

    second = await api.call("POST", f"/episodes/{episode_id}/production")
    assert second.status_code == 200, second.text
    assert second.json()["started"] is False
    assert second.json()["workflow_id"] == started["workflow_id"]
    assert await api.counts(episode_id) == before  # 予約・agent run・command は 1 件も増えない
    assert api.e2e.llm_calls == 3


async def test_an_ai_assistant_cannot_start_production(api: Api) -> None:
    episode_id = await api.draft()
    response = await api.call("POST", f"/episodes/{episode_id}/production", token=AI)
    assert response.status_code == 403, response.text
    assert _detail(response)["error"] == "forbidden"
    assert _detail(response)["reason"] == "human_only"
    assert not await api.workflow_exists(episode_id)
    assert api.e2e.llm_calls == 0
    assert (await api.counts(episode_id))["reservations"] == 0
    assert (await api.counts(episode_id))["agent_runs"] == 0


# --------------------------------------------------------------------------- 3. flag OFF


async def test_with_the_agentic_flag_off_start_and_resume_are_refused_before_anything_is_written(
    api: Api,
) -> None:
    pinned = await api.draft()
    await api.e2e.agentic.pin_workflow_plan(  # type: ignore[union-attr]
        _pin_request(pinned)
    )
    unpinned = await api.draft()
    api.set_flags(jpmas2_agentic_workflows=False)

    start = await api.call("POST", f"/episodes/{unpinned}/production")
    assert start.status_code == 404 and _detail(start)["error"] == "feature_disabled"
    assert not await api.workflow_exists(unpinned)

    before = await api.counts(pinned)
    resume = await api.call(
        "POST", f"/episodes/{pinned}/resume", body={"command_id": f"resume-{uuid.uuid4().hex[:8]}"}
    )
    assert resume.status_code == 404 and _detail(resume)["error"] == "feature_disabled"
    assert await api.counts(pinned) == before  # command 行を作らない
    assert await api.command_count(pinned, CommandType.RESUME) == 0
    assert not await api.workflow_exists(pinned)
    assert api.e2e.llm_calls == 0


def _pin_request(episode_id: str) -> Any:
    from contracts.agentic_pipeline import PinWorkflowPlanRequest

    return PinWorkflowPlanRequest(episode_id, "shorts_agentic", "1")


# --------------------------------------------------------------------------- 4. storyboard proposal


async def test_a_storyboard_proposal_is_recorded_as_proposed_and_does_not_apply(api: Api) -> None:
    episode_id = await api.produce()
    view = GetStoryboardResult.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/storyboard")).json()
    )
    board = view.storyboard
    assert board is not None and len(board.scenes) == 4 and board.revision_id
    assert board.editable_fields == ("visual_description",)
    current_before = await api.e2e.current(episode_id, ArtifactType.STORYBOARD)
    assert [str(r.id) for r in current_before] == [board.revision_id]

    new_text = "A wide shot of the old castle town at dawn"
    command_id = f"prop-{uuid.uuid4().hex[:10]}"
    body = {
        "command_id": command_id,
        "proposal": {
            "episode_id": episode_id,
            "base_storyboard_revision_id": board.revision_id,
            "edits": [{"scene_id": board.scenes[1].scene_id, "visual_description": new_text}],
        },
    }
    before = await api.counts(episode_id)
    response = await api.call("POST", f"/episodes/{episode_id}/storyboard/proposals", body=body)
    assert response.status_code == 202, response.text
    [proposed] = response.json()["commands"]
    assert proposed["created"] is True

    await api.wait_command(proposed["command_id"])
    mine = await api.e2e.rows(
        ChangeSetRow, ChangeSetRow.proposed_by_command_id == proposed["command_id"]
    )
    assert len(mine) == 1 and mine[0].status == "proposed"
    impact = ChangeImpactPreview.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/change-sets/{mine[0].id}/impact")).json()
    )
    assert impact.status == "proposed" and impact.decided_by_command_id is None

    # 提案は適用ではない: 現行の絵コンテは同じ revision のまま
    current_after = await api.e2e.current(episode_id, ArtifactType.STORYBOARD)
    assert [str(r.id) for r in current_after] == [board.revision_id]
    again_view = GetStoryboardResult.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/storyboard")).json()
    )
    assert again_view.storyboard is not None
    assert again_view.storyboard.revision_id == board.revision_id
    assert new_text not in {s.visual_description for s in again_view.storyboard.scenes}

    # 同じ提案の再送: 200、command も ChangeSet も増えない
    after_first = await api.counts(episode_id)
    assert after_first["commands"] == before["commands"] + 1
    resend = await api.call("POST", f"/episodes/{episode_id}/storyboard/proposals", body=body)
    assert resend.status_code == 200, resend.text
    assert [c["created"] for c in resend.json()["commands"]] == [False]
    assert await api.counts(episode_id) == after_first


async def test_a_storyboard_edit_that_needs_patch_scenes_is_unsupported(api: Api) -> None:
    episode_id = await api.produce()
    board = GetStoryboardResult.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/storyboard")).json()
    ).storyboard
    assert board is not None
    before = await api.counts(episode_id)
    response = await api.call(
        "POST",
        f"/episodes/{episode_id}/storyboard/proposals",
        body={
            "command_id": f"prop-{uuid.uuid4().hex[:10]}",
            "proposal": {
                "episode_id": episode_id,
                "base_storyboard_revision_id": board.revision_id,
                "edits": [{"scene_id": board.scenes[0].scene_id, "framing": "close-up"}],
            },
        },
    )
    assert response.status_code == 422, response.text
    assert _detail(response)["error"] == "unsupported_change"
    assert await api.counts(episode_id) == before  # 何も書かれない


# --------------------------------------------------------------------------- 5. apply guards


async def test_apply_is_refused_before_any_command_when_the_approval_does_not_cover_the_cost(
    api: Api,
) -> None:
    """QA agent が出した提案（proposed）を使う。適用は始めない（有料の再生成は走らせない）。"""
    episode_id = await api.produce()
    [change_set] = await api.e2e.rows(
        ChangeSetRow, ChangeSetRow.episode_id == uuid.UUID(episode_id)
    )
    path = f"/episodes/{episode_id}/change-sets/{change_set.id}"
    impact = ChangeImpactPreview.model_validate((await api.call("GET", f"{path}/impact")).json())
    assert impact.status == "proposed"

    before = await api.counts(episode_id)
    body = {"command_id": f"apply-{uuid.uuid4().hex[:10]}", "approved_max_cost_usd": 0.0}
    refused = await api.call("POST", f"{path}/apply", body=body)
    assert refused.status_code == 422, refused.text
    if impact.cost_estimated:
        assert _detail(refused)["error"] == "budget_exceeded"
        assert float(impact.estimated_cost_usd or "0") > 0
    else:
        # 未見積りを 0 円として通さない
        assert impact.estimated_cost_usd is None
        assert _detail(refused)["error"] == "cost_not_estimated"
    assert await api.counts(episode_id) == before
    assert await api.command_count(episode_id, CommandType.APPLY_CHANGE) == 0

    # AI が上限を十分大きくして適用を求めても、有料の変更は backend の経路で断られる
    ai_body = {"command_id": f"apply-{uuid.uuid4().hex[:10]}", "approved_max_cost_usd": 1000.0}
    ai = await api.call("POST", f"{path}/apply", token=AI, body=ai_body)
    if not impact.cost_estimated:
        assert ai.status_code == 422 and _detail(ai)["error"] == "cost_not_estimated"
        assert await api.command_count(episode_id, CommandType.APPLY_CHANGE) == 0
    else:  # 実データは未見積りなので通常ここには来ない（見積り付きは次のテスト）
        assert ai.status_code == 202, ai.text
    # どの場合も変更は適用されず、現行の絵コンテは動かない
    [still] = await api.e2e.rows(ChangeSetRow, ChangeSetRow.episode_id == uuid.UUID(episode_id))
    assert still.status == "proposed"
    assert len(await api.e2e.current(episode_id, ArtifactType.STORYBOARD)) == 1


# --------------------------------------------------------------------------- 6. hold / resume


async def test_hold_then_resume_are_accepted_once_and_resends_add_no_command(api: Api) -> None:
    episode_id = await api.produce()
    base = await api.counts(episode_id)

    hold_body = {"command_id": f"hold-{uuid.uuid4().hex[:10]}", "reason": "check the render"}
    hold = await api.call("POST", f"/episodes/{episode_id}/hold", body=hold_body)
    assert hold.status_code == 202, hold.text
    assert hold.json()["created"] is True and hold.json()["command_id"] == hold_body["command_id"]
    await api.wait_command(hold_body["command_id"])

    resume_body = {"command_id": f"resume-{uuid.uuid4().hex[:10]}"}
    resume = await api.call("POST", f"/episodes/{episode_id}/resume", body=resume_body)
    assert resume.status_code == 202, resume.text
    accepted = ResumeAccepted.model_validate(resume.json())
    assert accepted.pipeline_kind.value == "agentic"
    assert accepted.worker_check is WorkerCheck.POLLERS_PRESENT
    await api.wait_command(resume_body["command_id"])

    after = await api.counts(episode_id)
    assert after["commands"] == base["commands"] + 2

    hold_again = await api.call("POST", f"/episodes/{episode_id}/hold", body=hold_body)
    resume_again = await api.call("POST", f"/episodes/{episode_id}/resume", body=resume_body)
    assert hold_again.status_code == 200 and hold_again.json()["created"] is False
    assert resume_again.status_code == 200 and resume_again.json()["created"] is False
    assert resume_again.json()["pipeline_kind"] == "agentic"
    assert await api.counts(episode_id) == after
    # 完了・投稿済みの Episode の再開は何も走らせない（LLM・予約は増えない）
    assert api.e2e.llm_calls == 3 and after["reservations"] == 3


# --------------------------------------------------------------------------- 7. read models


async def test_decision_quality_and_progress_reads_match_their_contract_models(api: Api) -> None:
    episode_id = await api.produce()

    decisions = GetAgentDecisionsResult.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/agent-decisions")).json()
    )
    kinds = {d["decision_type"] for d in decisions.decisions}
    assert {"strategy_brief", "choose_topic", "creative_brief", "quality_review"} <= kinds
    assert {r["role"] for r in decisions.agent_runs} >= NEW_ROLES - {"learning"}

    quality = GetQualityReviewResult.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/quality-review")).json()
    )
    assert quality.status == "reviewed" and quality.review is not None
    assert quality.artifact_id is not None
    assert len(quality.review.findings) == 2

    progress = WorkflowProgress.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/progress")).json()
    )
    assert progress.workflow_id == agentic_episode_workflow_id(episode_id)
    assert progress.pipeline_kind.value == "agentic"
    assert progress.failure is None

    # 未知の Episode / 認証なしは 404 / 401（読み取りでも）
    missing = await api.call("GET", f"/episodes/{uuid.uuid4()}/progress")
    assert missing.status_code == 404 and _detail(missing)["error"] == "episode_not_found"
    anonymous = await api.http.get(f"{PREFIX}/episodes")
    assert anonymous.status_code == 401


# --------------------------------------------------------------------------- 8. brief change


async def test_a_changed_creative_brief_makes_a_new_script_and_no_duplicate_reservation(
    pg_session_factory, minio_store
) -> None:
    """P1 既知問題 A を実 PostgreSQL + 実 MinIO で: 成功済みの台本のあとに brief が変わる。"""
    from infrastructure.db.repositories import (
        ArtifactMetadataRepository,
        ProviderReservationRepository,
    )
    from tests.support.fakes import FakeStoryGenerator
    from tests.unit.test_script_brief_revision import _write_brief
    from tests.unit.test_script_creative_brief import _activities, _plain_episode
    from tests.unit.test_script_regeneration_after_brief_change import (
        _jobs,
        _numbered_script,
        _run_script_stage,
    )

    generator = FakeStoryGenerator(output=lambda _r: _numbered_script(len(generator.requests)))
    acts = _activities(pg_session_factory, minio_store, generator)
    episode_id = await _plain_episode(pg_session_factory, "draft")
    await _write_brief(pg_session_factory, minio_store, episode_id)
    first_job, first = await _run_script_stage(acts, episode_id)

    async def reservations() -> list[Any]:
        async with pg_session_factory() as s:
            return await ProviderReservationRepository(s).list_for_episode_provider(
                episode_id, ProviderCall.CODEX_SCRIPT
            )

    assert len(await reservations()) == 1

    await _write_brief(
        pg_session_factory, minio_store, episode_id, hook="A different opening line here."
    )
    second_job, second = await _run_script_stage(acts, episode_id)
    assert second_job != first_job and second.reused is False
    assert len(await reservations()) == 2  # 新しい brief は新しい予約
    jobs = await _jobs(pg_session_factory, episode_id)
    assert {j.id for j in jobs} == {first_job, second_job}

    async with pg_session_factory() as s:
        superseded = {
            row.id: row.superseded_at
            for row in await s.scalars(
                select(ArtifactMetadataRow).where(
                    ArtifactMetadataRow.episode_id == uuid.UUID(episode_id),
                    ArtifactMetadataRow.artifact_type == ArtifactType.SCRIPT.value,
                )
            )
        }
        repo = ArtifactMetadataRepository(s)
        scripts = [
            a
            for a in await repo.list_for_episode(episode_id)
            if a.artifact_type is ArtifactType.SCRIPT
        ]
        current = await repo.find_current_by_type(episode_id, ArtifactType.SCRIPT)
    assert {a.id for a in scripts} == {first.artifact_id, second.artifact_id}
    assert current is not None and current.id == second.artifact_id
    # 旧版の行は消えず superseded として残り、現行だけが superseded でない
    assert superseded[uuid.UUID(first.artifact_id)] is not None
    assert superseded[uuid.UUID(second.artifact_id)] is None

    # 同じ brief の再実行: job も予約も生成も増えない（二重課金なし）
    third_job, third = await _run_script_stage(acts, episode_id)
    assert third_job == second_job and third.reused is True
    assert len(await reservations()) == 2 and len(generator.requests) == 2


async def test_a_paid_change_needs_the_estimate_and_an_ai_cannot_apply_it(api: Api) -> None:
    """見積り付きの ChangeSet（store に直接 seed。QA の提案は未見積り）で費用と AI を確かめる。

    人の承認が見積り未満なら command を作らない。AI は上限を大きくしても適用できない。
    人が十分な上限で承認する経路は有料の再生成を始めるので、ここでは通さない。
    """
    from decimal import Decimal

    from contracts.intervention import ActorType, ChangeKind
    from infrastructure.intervention.store import Actor, InterventionStore

    async with api.e2e.sf() as session:
        episode_id = await make_episode(session, EpisodeStatus.STORYBOARD_READY)
        await seed_script_and_storyboard(session, api.e2e.store, episode_id)
    board = GetStoryboardResult.model_validate(
        (await api.call("GET", f"/episodes/{episode_id}/storyboard")).json()
    ).storyboard
    assert board is not None
    async with api.e2e.sf() as session:
        paid = await InterventionStore(session).insert_change_set(
            episode_id=uuid.UUID(episode_id),
            kind=ChangeKind.SCENE_OVERRIDE_VISUAL,
            base_revision_id=board.revision_id,
            proposal={
                "change": {
                    "kind": ChangeKind.SCENE_OVERRIDE_VISUAL.value,
                    "base_revision_id": board.revision_id,
                    "scene_id": board.scenes[0].scene_id,
                    "visual_description": "A calmer wide shot of the harbor",
                }
            },
            actor=Actor(ActorType.AI_ASSISTANT, "chat-assistant"),
            proposed_by_command_id=f"seed-{uuid.uuid4().hex[:10]}",
            estimated_cost_usd=Decimal("0.30"),
        )
        await session.commit()
    path = f"/episodes/{episode_id}/change-sets/{paid.change_set_id}"
    impact = ChangeImpactPreview.model_validate((await api.call("GET", f"{path}/impact")).json())
    assert impact.cost_estimated and impact.estimated_cost_usd == "0.3000"

    before = await api.counts(episode_id)
    low = await api.call(
        "POST",
        f"{path}/apply",
        body={"command_id": f"apply-{uuid.uuid4().hex[:10]}", "approved_max_cost_usd": 0.1},
    )
    assert low.status_code == 422 and _detail(low)["error"] == "budget_exceeded", low.text
    assert await api.counts(episode_id) == before

    ai_id = f"apply-{uuid.uuid4().hex[:10]}"
    ai = await api.call(
        "POST",
        f"{path}/apply",
        token=AI,
        body={"command_id": ai_id, "approved_max_cost_usd": 5.0},
    )
    assert ai.status_code == 202, ai.text
    cmd = await api.wait_command(ai_id, want="rejected")
    assert cmd["reject_reason"] == "forbidden", cmd
    assert cmd["result"]["detail"] == "ai_paid_apply"
    [still] = await api.e2e.rows(ChangeSetRow, ChangeSetRow.id == uuid.UUID(paid.change_set_id))
    assert still.status == "proposed"  # 適用されていない
    current = await api.e2e.current(episode_id, ArtifactType.STORYBOARD)
    assert [str(r.id) for r in current] == [board.revision_id]
    # 有料の再生成は始まらない（seed した素材の予約は前のまま）
    assert (await api.counts(episode_id))["reservations"] == before["reservations"]
