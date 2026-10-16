"""ResearchWorkflow と research worker（ADR-0037 §8.5）。

time-skipping テストサーバ + 本物の ``ResearchWorkflow`` + 本物の ``ResearchActivities``
（``ResearchExecutor`` を SQLite・インメモリ ArtifactStore・Fake Provider で組む）。

守るもの:
- 依頼は Activity（executor）が DB に記録し、workflow は参照と件数だけを返す（履歴に本体を載せない）
- Provider ``none`` は外部を呼ばずに ``blocked``
- 一時障害は上限つき（``RESEARCH_EXECUTE_MAX_ATTEMPTS``）で retry し、使い切ったら
  ``failed`` を記録する
  （retry も台帳の枠を数える。INV-36）
- research の non-retryable（基底の表 ∪ research の表）は retry しない。
  ``needs_input`` は ``blocked``、``permanent`` は ``failed``
- 同じ依頼の workflow を再実行しても外部呼び出しを送り直さない

理由は docs/testing/research-worker-rationale.md。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import fields
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from contracts.research import (
    RESEARCH_EXECUTE_ACTIVITY,
    RESEARCH_EXECUTE_MAX_ATTEMPTS,
    RESEARCH_TASK_QUEUE,
    RESEARCH_WORKFLOW_NAME,
    ResearchCall,
    ResearchExecuteRequest,
    ResearchStatus,
    ResearchWorkflowInput,
    ResearchWorkflowOutput,
    parse_research_spec,
    research_workflow_id,
)
from domain.errors import NON_RETRYABLE_ERROR_TYPE_NAMES
from domain.research.errors import (
    RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES,
    RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES,
    ResearchAmbiguousCallError,
    ResearchInputInvalidError,
    ResearchOutputInvalidError,
)
from infrastructure.config import Settings
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchCallRepository,
    ResearchRequestRepository,
)
from infrastructure.research.errors import ProviderTransient
from infrastructure.research.executor import ResearchExecutor
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.registry import CostModel, ResearchProviders
from tests.support.research import evidence_payload
from tests.support.research_handlers import GenericTestHandler
from workers.research.activities import ResearchActivities
from workers.research.run_worker import build_activities, build_worker
from workers.research.workflows import (
    EXECUTE_RETRY_POLICY,
    STATE_RETRY_POLICY,
    ResearchWorkflow,
)

PCV = "provider-config-1+fake"
TWO = ("明治維新", "鉄砲伝来")
OUTPUT_FIELDS = {f.name for f in fields(ResearchWorkflowOutput)}


class _FailingSearch(FakeSearchProvider):
    """指定した検索語の最初の ``times`` 回だけ ``error`` を投げる（それ以外は Fake のまま）。"""

    def __init__(self, text: str, error: BaseException, times: int) -> None:
        super().__init__()
        self._text, self._error, self._left = text, error, times

    async def search(self, query):  # type: ignore[override]
        if query.text == self._text and self._left > 0:
            self._left -= 1
            self.calls.append(query)
            raise self._error
        return await super().search(query)


@pytest_asyncio.fixture
async def wf_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def _request(session_factory, key: str = "evidence:wf") -> str:
    async with session_factory() as session:
        created = await ResearchRequestRepository(session).create_or_get(
            idempotency_key=key,
            spec=parse_research_spec(evidence_payload()),
            provider_config_version=PCV,
        )
        await session.commit()
    return created.id


def _activities(
    session_factory,
    store,
    *,
    search: FakeSearchProvider | None = None,
    configured: bool = True,
) -> ResearchActivities:
    handler = GenericTestHandler(TWO)
    providers = ResearchProviders(
        mode="fake" if configured else "none",
        search=(search or FakeSearchProvider()) if configured else None,
        fetcher=FakeContentFetcher() if configured else None,
        is_real=False,
    )
    executor = ResearchExecutor(
        session_factory=session_factory,
        store=store,
        bucket="artifacts",
        providers=providers,
        handlers={handler.kind: handler},
        cost_model=CostModel(),
    )
    return ResearchActivities(executor)


async def _run(client: Client, queue: str, request_id: str) -> ResearchWorkflowOutput:
    return await client.execute_workflow(
        RESEARCH_WORKFLOW_NAME,
        ResearchWorkflowInput(request_id=request_id),
        id=f"{research_workflow_id(request_id)}-{uuid.uuid4().hex[:8]}",
        task_queue=queue,
        result_type=ResearchWorkflowOutput,
    )


def _queue() -> str:
    return f"research-test-{uuid.uuid4()}"


async def _calls(session_factory, request_id: str):
    async with session_factory() as session:
        return await ResearchCallRepository(session).list_for_request(request_id)


async def _stored(session_factory, request_id: str):
    async with session_factory() as session:
        return await ResearchRequestRepository(session).get(request_id)


# ------------------------------------------------------------------ 正常系


async def test_a_queued_request_completes_and_the_history_carries_only_refs_and_counts(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    queue = _queue()
    acts = _activities(session_factory, artifact_store)
    workflow_id = f"{research_workflow_id(request_id)}-ok"
    async with build_worker(wf_env.client, acts, task_queue=queue):
        handle = await wf_env.client.start_workflow(
            RESEARCH_WORKFLOW_NAME,
            ResearchWorkflowInput(request_id=request_id),
            id=workflow_id,
            task_queue=queue,
            result_type=ResearchWorkflowOutput,
        )
        output = await handle.result()

    assert output.status == ResearchStatus.COMPLETED.value
    assert output.stop_code is None
    assert (output.searches, output.fetches) == (2, 2)
    (ref,) = output.artifact_refs
    async with session_factory() as session:
        current = await ResearchArtifactRepository(session).list_current(request_id)
    assert [(a.id, a.sha256) for a in current] == [(ref.artifact_id, ref.sha256)]
    stored = await _stored(session_factory, request_id)
    assert stored is not None and stored.status is ResearchStatus.COMPLETED

    # 履歴に載る Activity の結果は参照と件数だけ（成果物の本体・検索結果・本文を載せない）
    history = await handle.fetch_history()
    results: list[dict[str, Any]] = []
    for event in history.events:
        if event.HasField("activity_task_completed_event_attributes"):
            for payload in event.activity_task_completed_event_attributes.result.payloads:
                results.append(json.loads(payload.data))
    assert results, "Activity の結果が履歴に無い"
    for result in results:
        assert set(result) <= OUTPUT_FIELDS
        for pointer in result["artifact_refs"]:
            assert set(pointer) == {"artifact_type", "artifact_id", "sha256"}


async def test_re_running_the_workflow_for_a_finished_request_makes_no_new_calls(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    queue = _queue()
    async with build_worker(
        wf_env.client, _activities(session_factory, artifact_store), task_queue=queue
    ):
        first = await _run(wf_env.client, queue, request_id)
    calls_before = await _calls(session_factory, request_id)

    search = FakeSearchProvider()
    async with build_worker(
        wf_env.client, _activities(session_factory, artifact_store, search=search), task_queue=queue
    ):
        again = await _run(wf_env.client, queue, request_id)

    assert again == first
    assert search.calls == []
    assert await _calls(session_factory, request_id) == calls_before


# ------------------------------------------------------------------ fail-closed


async def test_provider_none_blocks_the_request_without_any_call(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    queue = _queue()
    async with build_worker(
        wf_env.client,
        _activities(session_factory, artifact_store, configured=False),
        task_queue=queue,
    ):
        output = await _run(wf_env.client, queue, request_id)

    assert output.status == ResearchStatus.BLOCKED.value
    assert output.stop_code == "provider_not_configured"
    assert output.artifact_refs == []
    assert await _calls(session_factory, request_id) == []


async def test_the_default_worker_settings_build_a_worker_that_blocks(
    wf_env, session_factory, artifact_store
) -> None:
    """既定の ``RESEARCH_PROVIDER``（``none``）で組んだ worker は外部を呼ばずに ``blocked``。"""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.research_provider == "none"
    acts = build_activities(settings, session_factory=session_factory, store=artifact_store)
    request_id = await _request(session_factory)
    queue = _queue()
    async with build_worker(wf_env.client, acts, task_queue=queue):
        output = await _run(wf_env.client, queue, request_id)
    assert output.status == ResearchStatus.BLOCKED.value
    assert output.stop_code == "provider_not_configured"


# ------------------------------------------------------------------ retry（上限つき）


async def test_a_transient_failure_that_recovers_is_retried_and_completes(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    search = _FailingSearch("鉄砲伝来", ProviderTransient("connection reset"), times=1)
    queue = _queue()
    async with build_worker(
        wf_env.client, _activities(session_factory, artifact_store, search=search), task_queue=queue
    ):
        output = await _run(wf_env.client, queue, request_id)

    assert output.status == ResearchStatus.COMPLETED.value
    texts = [q.text for q in search.calls]
    assert texts.count("明治維新") == 1  # 成功済みの検索は送り直さない
    assert texts.count("鉄砲伝来") == 2


async def test_transient_failures_are_retried_a_bounded_number_of_times_then_recorded_failed(
    wf_env, session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    search = _FailingSearch("鉄砲伝来", ProviderTransient("connection reset"), times=100)
    queue = _queue()
    async with build_worker(
        wf_env.client, _activities(session_factory, artifact_store, search=search), task_queue=queue
    ):
        output = await _run(wf_env.client, queue, request_id)

    assert output.status == ResearchStatus.FAILED.value
    assert output.stop_code == "execution_failed"
    assert [q.text for q in search.calls].count("鉄砲伝来") == RESEARCH_EXECUTE_MAX_ATTEMPTS
    searches = [
        c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.SEARCH
    ]
    # retry も台帳の枠を数える（INV-36）。成功済みの 1 件 + 失敗した試行の数
    assert len(searches) == 1 + RESEARCH_EXECUTE_MAX_ATTEMPTS
    stored = await _stored(session_factory, request_id)
    assert stored is not None and stored.status is ResearchStatus.FAILED
    assert stored.result_summary is not None
    assert "ResearchSourceUnavailableError" in stored.result_summary["warnings"][0]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ResearchAmbiguousCallError, ResearchStatus.BLOCKED),  # needs_input（research の表）
        (ResearchInputInvalidError, ResearchStatus.FAILED),  # permanent（research の表）
        (ResearchOutputInvalidError, ResearchStatus.FAILED),
    ],
)
async def test_a_non_retryable_research_error_is_not_retried_and_is_recorded(
    wf_env, session_factory, artifact_store, error: type[Exception], status: ResearchStatus
) -> None:
    request_id = await _request(session_factory)
    real = _activities(session_factory, artifact_store)
    attempts: list[int] = []

    @activity.defn(name=RESEARCH_EXECUTE_ACTIVITY)
    async def failing_execute(request: ResearchExecuteRequest) -> ResearchWorkflowOutput:
        attempts.append(activity.info().attempt)
        raise error(f"injected for {request.request_id}")

    queue = _queue()
    async with build_worker(
        wf_env.client,
        real,
        task_queue=queue,
        activities_override=[failing_execute, real.record_failure],
    ):
        output = await _run(wf_env.client, queue, request_id)

    assert attempts == [1]  # retry しない（基底の表 ∪ research の表）
    assert output.status == status.value
    assert output.stop_code == "execution_failed"
    stored = await _stored(session_factory, request_id)
    assert stored is not None and stored.status is status


# ------------------------------------------------------------------ 境界の定義


def test_the_execute_retry_policy_is_bounded_and_uses_both_error_tables() -> None:
    assert EXECUTE_RETRY_POLICY.maximum_attempts == RESEARCH_EXECUTE_MAX_ATTEMPTS
    names = set(EXECUTE_RETRY_POLICY.non_retryable_error_types or [])
    assert names == set(RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES)
    assert set(NON_RETRYABLE_ERROR_TYPE_NAMES) <= names
    assert set(RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES) <= names
    assert "ResearchAmbiguousCallError" in names
    assert "ResearchSourceUnavailableError" not in names  # retryable は retry する
    assert set(STATE_RETRY_POLICY.non_retryable_error_types or []) == names
    assert STATE_RETRY_POLICY.maximum_attempts and STATE_RETRY_POLICY.maximum_attempts > 1


async def test_the_worker_listens_on_the_contract_queue(
    wf_env, session_factory, artifact_store
) -> None:
    worker = build_worker(wf_env.client, _activities(session_factory, artifact_store))
    assert worker.task_queue == RESEARCH_TASK_QUEUE
    assert ResearchWorkflow in worker.config().get("workflows", [])
