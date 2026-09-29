"""Trend Research を worker の設定どおりに組み、ResearchWorkflow で走らせる（ADR-0039）。

time-skipping テストサーバ + 本物の ``ResearchWorkflow`` + registry が組む Activity
（``RESEARCH_PROVIDER=fake``: Fake 検索・Fake 解釈器・``TrendHandler``）。

守るもの:
- ``fake`` で組んだ worker は Trend を ``handler_not_available`` で止めず、成果物まで作る
- 履歴に載るのは参照と件数だけ（解釈の件数も台帳から数える）。本文は取得しない
- 保存された Trend は ``latest_trend`` で検証つきで読み戻せる（B6 の読み口）
- ``none`` の worker は外部を呼ばずに ``blocked``

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from contracts.research import (
    RESEARCH_WORKFLOW_NAME,
    ResearchArtifactType,
    ResearchStatus,
    ResearchWorkflowInput,
    ResearchWorkflowOutput,
    research_workflow_id,
)
from infrastructure.config import Settings
from infrastructure.research.fake_trend import FakeTrendInterpreter
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from infrastructure.research.registry import build_providers
from tests.support.research_trend import make_trend_request, stored_trend
from workers.research.run_worker import build_activities, build_worker


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, research_provider=mode)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def wf_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def _run(env, session_factory, store, request_id: str, mode: str = "fake"):
    acts = build_activities(_settings(mode), session_factory=session_factory, store=store)
    queue = f"research-test-{uuid.uuid4()}"
    async with build_worker(env.client, acts, task_queue=queue):
        return await env.client.execute_workflow(
            RESEARCH_WORKFLOW_NAME,
            ResearchWorkflowInput(request_id=request_id),
            id=f"{research_workflow_id(request_id)}-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
            result_type=ResearchWorkflowOutput,
        )


def test_the_registry_builds_the_fake_interpreter_only_for_fake() -> None:
    assert isinstance(build_providers(_settings("fake")).interpreter, FakeTrendInterpreter)
    assert build_providers(_settings("none")).interpreter is None


async def test_a_fake_worker_runs_trend_to_a_completed_verified_artifact(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await make_trend_request(session_factory)
    output = await _run(wf_env, session_factory, artifact_store, request_id)

    assert output.status == ResearchStatus.COMPLETED.value, output
    assert output.stop_code is None
    assert [p.artifact_type for p in output.artifact_refs] == [
        ResearchArtifactType.RESEARCH_TREND.value
    ]
    assert output.searches >= 1 and output.assessments == 1 and output.fetches == 0

    record, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert output.artifact_refs[0].sha256 == record.sha256
    assert artifact.observations and artifact.interpretations

    gateway = ResearchGateway(
        session_factory=session_factory,
        store=artifact_store,
        config=GatewayConfig(
            provider_mode="fake", provider_is_real=False, provider_configured=True
        ),
    )
    latest = await gateway.latest_trend(channel_id="channel-1", region="JP", language="ja")
    assert latest is not None and latest.request_id == request_id
    assert latest.artifact == artifact


async def test_a_none_worker_blocks_a_trend_without_calls(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await make_trend_request(session_factory)
    output = await _run(wf_env, session_factory, artifact_store, request_id, mode="none")
    assert output.status == ResearchStatus.BLOCKED.value
    assert output.stop_code == "provider_not_configured"
    assert output.searches == output.assessments == 0
