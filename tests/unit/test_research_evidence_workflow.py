"""Evidence Research を worker の設定どおりに組み、ResearchWorkflow で走らせる（ADR-0038）。

time-skipping テストサーバ + 本物の ``ResearchWorkflow`` + registry が組む Activity
（``RESEARCH_PROVIDER=fake``: Fake 検索・Fake 取得・Fake 評価器・``EvidenceHandler``）。

守るもの:
- ``fake`` で組んだ worker は Evidence を ``handler_not_available`` で止めず、成果物まで作る
- 履歴に載るのは参照と件数だけ（評価の件数も台帳から数える）
- 評価の枠（``max_assessments``）が足りなければ ``partial``（合格にしない）
- registry は ``fake`` のときだけ Fake の評価器・抽出器を組み、``none`` は組まない

理由は docs/testing/research-evidence-rationale.md。
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
from infrastructure.research.fake_evidence import FakeClaimExtractor, FakeEvidenceAssessor
from infrastructure.research.registry import build_claim_extractor, build_providers
from tests.support.research_evidence import (
    MEIJI_CLAIM,
    TANEGASHIMA_CLAIM,
    claims_payload,
    make_request,
    stored_evidence,
)
from workers.research.run_worker import build_activities, build_worker


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, research_provider=mode)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def wf_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def _run(env, session_factory, store, request_id: str) -> ResearchWorkflowOutput:
    acts = build_activities(_settings("fake"), session_factory=session_factory, store=store)
    queue = f"research-test-{uuid.uuid4()}"
    async with build_worker(env.client, acts, task_queue=queue):
        return await env.client.execute_workflow(
            RESEARCH_WORKFLOW_NAME,
            ResearchWorkflowInput(request_id=request_id),
            id=f"{research_workflow_id(request_id)}-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
            result_type=ResearchWorkflowOutput,
        )


def test_the_registry_builds_fake_llm_ports_only_for_fake() -> None:
    fake = build_providers(_settings("fake"))
    assert isinstance(fake.assessor, FakeEvidenceAssessor)
    assert isinstance(build_claim_extractor(_settings("fake")), FakeClaimExtractor)
    none = build_providers(_settings("none"))
    assert none.assessor is None and build_claim_extractor(_settings("none")) is None


async def test_a_fake_worker_runs_evidence_to_a_completed_artifact(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    output = await _run(wf_env, session_factory, artifact_store, request_id)

    assert output.status == ResearchStatus.COMPLETED.value
    assert output.stop_code is None
    assert [r.artifact_type for r in output.artifact_refs] == [
        ResearchArtifactType.RESEARCH_EVIDENCE.value
    ]
    assert output.searches >= 1 and output.fetches >= 1 and output.assessments == 1
    record, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    assert output.artifact_refs[0].sha256 == record.sha256
    assert artifact.claims[0].assessed


async def test_an_assessment_ceiling_makes_the_workflow_partial(
    wf_env, session_factory, artifact_store
) -> None:
    payload = claims_payload(TANEGASHIMA_CLAIM, MEIJI_CLAIM, limits={"max_assessments": 1})
    request_id = await make_request(session_factory, payload)
    output = await _run(wf_env, session_factory, artifact_store, request_id)

    assert output.status == ResearchStatus.PARTIAL.value
    assert output.assessments == 1
    _, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    assert any(not c.assessed for c in artifact.claims)
