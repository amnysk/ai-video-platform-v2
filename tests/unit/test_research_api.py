"""Research API（ADR-0037 §8.5 / INV-16 / INV-8）と ``ResearchWorkflow`` の起動。

- ``POST /research/requests``: Gateway の受け付け（冪等キー・予算の門・鮮度キャッシュ）の後、
  ``queued`` の依頼だけ workflow を起動し、完了を待たずに 202
- ``blocked``（Provider ``none``）・再利用した依頼は起動しない
- ``POST /research/requests/{id}/resume``: 門を通った ``blocked`` だけ ``queued`` に戻して起動する
- ``GET /research/requests/{id}``: DB だけを読む
- 起動は依頼 1 件につき 1 つの workflow id。実行中の id の拒否は成功として扱う

Temporal・MinIO には接続しない（依存を override する。INV-18）。
理由は docs/testing/research-worker-rationale.md。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.research import (
    RESEARCH_TASK_QUEUE,
    RESEARCH_WORKFLOW_NAME,
    ResearchKind,
    ResearchStatus,
    ResearchWorkflowInput,
    research_workflow_id,
)
from infrastructure.config import Settings
from infrastructure.db.research_repositories import ResearchCallRepository
from infrastructure.research.executor import ResearchExecutor
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.registry import CostModel, ResearchProviders
from infrastructure.temporal.research_starter import start_research_workflow
from tests.support.research import evidence_payload
from tests.support.research_handlers import GenericTestHandler


class FakeResearchStarter:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_research(self, *, request_id: str) -> str:
        self.started.append(request_id)
        return research_workflow_id(request_id)


class Api:
    def __init__(self, client: AsyncClient, starter: FakeResearchStarter, app: Any) -> None:
        self.client = client
        self.starter = starter
        self.app = app

    def use_provider(self, mode: str) -> None:
        from apps.api.dependencies import get_settings

        settings = Settings(_env_file=None, research_provider=mode)  # type: ignore[call-arg]
        self.app.dependency_overrides[get_settings] = lambda: settings


@pytest_asyncio.fixture
async def api(session_factory, artifact_store):
    from apps.api.dependencies import get_session_factory
    from apps.api.main import create_app
    from apps.api.routers.research import get_research_starter, get_research_store

    starter = FakeResearchStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_research_store] = lambda: artifact_store
    app.dependency_overrides[get_research_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        harness = Api(client, starter, app)
        harness.use_provider("fake")
        yield harness


def _body(key: str = "evidence:api-1", **overrides: Any) -> dict[str, Any]:
    return {**evidence_payload(**overrides), "idempotency_key": key}


# ------------------------------------------------------------------ POST /research/requests


async def test_submitting_a_request_stores_it_and_starts_the_workflow_without_waiting(
    api: Api,
) -> None:
    response = await api.client.post("/research/requests", json=_body())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kind"] == ResearchKind.EVIDENCE.value
    assert body["status"] == ResearchStatus.QUEUED.value
    assert body["reused"] is False
    assert body["workflow_id"] == research_workflow_id(body["request_id"])
    assert api.starter.started == [body["request_id"]]

    view = await api.client.get(f"/research/requests/{body['request_id']}")
    assert view.status_code == 200
    assert view.json()["status"] == ResearchStatus.QUEUED.value


async def test_resubmitting_the_same_key_returns_the_same_request_and_restarts_it(
    api: Api,
) -> None:
    """保存と起動の間で落ちた依頼も、同じキーの再 POST で同じ workflow id から起動し直せる。"""
    first = (await api.client.post("/research/requests", json=_body())).json()
    second = (await api.client.post("/research/requests", json=_body())).json()
    assert second["request_id"] == first["request_id"]
    assert second["workflow_id"] == first["workflow_id"]
    assert api.starter.started == [first["request_id"], first["request_id"]]


async def test_the_same_key_with_a_different_meaning_is_a_conflict(api: Api) -> None:
    await api.client.post("/research/requests", json=_body())
    other = _body(audience="someone else")
    response = await api.client.post("/research/requests", json=other)
    assert response.status_code == 409
    assert len(api.starter.started) == 1


async def test_an_invalid_request_is_rejected_before_anything_is_stored(api: Api) -> None:
    body = _body()
    body["inputs"] = {"region": "US", "audience_hypothesis": "x"}  # Trend の入力を Evidence に
    response = await api.client.post("/research/requests", json=body)
    assert response.status_code == 422
    assert api.starter.started == []


async def test_provider_none_blocks_at_submit_and_starts_nothing(api: Api) -> None:
    api.use_provider("none")
    response = await api.client.post("/research/requests", json=_body())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == ResearchStatus.BLOCKED.value
    assert body["workflow_id"] is None
    assert api.starter.started == []
    view = (await api.client.get(f"/research/requests/{body['request_id']}")).json()
    assert view["blocked_reason"]["code"] == "provider_not_configured"
    assert view["result"]["stop_code"] == "provider_not_configured"


async def test_a_fresh_completed_request_is_reused_without_starting_a_workflow(
    api: Api, session_factory, artifact_store
) -> None:
    first = (await api.client.post("/research/requests", json=_body("evidence:a"))).json()
    handler = GenericTestHandler(("明治維新",))
    executor = ResearchExecutor(
        session_factory=session_factory,
        store=artifact_store,
        bucket="artifacts",
        providers=ResearchProviders(
            mode="fake", search=FakeSearchProvider(), fetcher=FakeContentFetcher(), is_real=False
        ),
        handlers={handler.kind: handler},
        cost_model=CostModel(),
    )
    done = await executor.execute(first["request_id"])
    assert done.status is ResearchStatus.COMPLETED

    response = await api.client.post("/research/requests", json=_body("evidence:b"))
    body = response.json()
    assert response.status_code == 202
    assert body["reused"] is True
    assert body["request_id"] == first["request_id"]
    assert body["status"] == ResearchStatus.COMPLETED.value
    assert body["workflow_id"] is None
    assert api.starter.started == [first["request_id"]]  # 再利用は起動しない


# ------------------------------------------------------------------ resume


async def test_resume_of_a_request_still_without_provider_is_a_conflict(api: Api) -> None:
    api.use_provider("none")
    blocked = (await api.client.post("/research/requests", json=_body())).json()
    response = await api.client.post(f"/research/requests/{blocked['request_id']}/resume")
    assert response.status_code == 409
    assert "provider_not_configured" in response.json()["detail"]
    assert api.starter.started == []


async def test_resume_after_the_provider_is_configured_requeues_and_starts(api: Api) -> None:
    api.use_provider("none")
    blocked = (await api.client.post("/research/requests", json=_body())).json()
    api.use_provider("fake")

    response = await api.client.post(f"/research/requests/{blocked['request_id']}/resume")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == ResearchStatus.QUEUED.value
    assert body["workflow_id"] == research_workflow_id(blocked["request_id"])
    assert api.starter.started == [blocked["request_id"]]
    again = await api.client.post(f"/research/requests/{blocked['request_id']}/resume")
    assert again.status_code == 409  # queued はもう blocked ではない
    assert api.starter.started == [blocked["request_id"]]


async def test_resume_and_get_of_an_unknown_request_are_404(api: Api) -> None:
    unknown = uuid.uuid4()
    assert (await api.client.post(f"/research/requests/{unknown}/resume")).status_code == 404
    assert (await api.client.get(f"/research/requests/{unknown}")).status_code == 404
    assert api.starter.started == []


async def test_get_reads_the_database_only(api: Api, session_factory) -> None:
    created = (await api.client.post("/research/requests", json=_body())).json()
    before = list(api.starter.started)
    view = (await api.client.get(f"/research/requests/{created['request_id']}")).json()
    assert view["request_id"] == created["request_id"]
    assert view["kind"] == "evidence"
    assert view["result"] is None
    assert api.starter.started == before
    async with session_factory() as session:
        assert await ResearchCallRepository(session).list_for_request(created["request_id"]) == []


# ------------------------------------------------------------------ 起動（Temporal の client）


class _RecordingClient:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.running:
            raise WorkflowAlreadyStartedError(kwargs["id"], RESEARCH_WORKFLOW_NAME)


REQUEST_ID = "7d1c3f5e-8a6b-4c2d-9e0f-1a2b3c4d5e6f"


async def test_the_starter_uses_one_workflow_id_per_request_on_the_research_queue() -> None:
    client = _RecordingClient()
    workflow_id = await start_research_workflow(client, request_id=REQUEST_ID)  # type: ignore[arg-type]

    assert workflow_id == research_workflow_id(REQUEST_ID)
    ((args, kwargs),) = client.calls
    assert args == (RESEARCH_WORKFLOW_NAME, ResearchWorkflowInput(request_id=REQUEST_ID))
    assert kwargs["id"] == workflow_id
    assert kwargs["task_queue"] == RESEARCH_TASK_QUEUE
    # blocked で成功終了した id でも再開できる（実行中の id は Temporal が拒否する）
    assert kwargs["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE


async def test_an_already_running_workflow_is_not_started_twice_and_is_not_an_error() -> None:
    client = _RecordingClient(running=True)
    workflow_id = await start_research_workflow(client, request_id=REQUEST_ID)  # type: ignore[arg-type]
    assert workflow_id == research_workflow_id(REQUEST_ID)
    assert len(client.calls) == 1


async def test_the_research_router_only_adds_paths_under_research(api: Api) -> None:
    """Research のルータは ``/research`` の下だけに足す（Episode のパスを足さない・変えない）。"""
    paths = set(api.app.openapi()["paths"])
    assert {p for p in paths if p.startswith("/research")} == {
        "/research/requests",
        "/research/requests/{request_id}",
        "/research/requests/{request_id}/resume",
    }
    assert not any("research" in p for p in paths if not p.startswith("/research"))
    assert "/episodes/{episode_id}/script" not in paths  # ADR-0037 §9（移植しない）
