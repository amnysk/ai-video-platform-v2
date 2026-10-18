"""``OPERATIONS`` と HTTP route の一致（INV-52、ADR-0044 §4）。

理由は docs/testing/jpmas2-p2a-http-rationale.md。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from application.errors import OperationError, OperationErrorCode
from contracts.application_operations import (
    OPERATIONS,
    ListEpisodesRequest,
    ListToolCallsRequest,
    OperationName,
    OperationSpec,
)
from contracts.assistant_tools import CORRELATION_HEADER
from contracts.intervention import ActorType
from contracts.states import EpisodeStatus
from tests.contract.test_application_contract_sync import _model_schema, _resolve
from tests.support.application_http import (
    AUTH,
    BODIES,
    CHANGE_SET,
    EPISODE,
    TOOL_CALL,
    FakeApplicationService,
    build_app,
    results,
    url,
)

PREFIX = "/v1/intervention"
IDS = [o.name.value for o in OPERATIONS]
SPECS = pytest.mark.parametrize("spec", OPERATIONS, ids=IDS)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _send(client: httpx.AsyncClient, spec: OperationSpec, **kw: Any) -> Any:
    body = BODIES.get(spec.name)
    return client.request(spec.http_method, url(spec), json=body, **kw)


def _flat_routes(app: FastAPI) -> list[Any]:
    """app の全 route（include された router の中身まで。FastAPI は include を入れ子で持つ）。"""
    out: list[Any] = []
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        out.extend(inner.routes if inner is not None else [route])
    return out


def _routes(app: FastAPI, spec: OperationSpec) -> list[Any]:
    return [
        r
        for r in _flat_routes(app)
        if getattr(r, "path", None) == PREFIX + spec.http_path
        and spec.http_method in getattr(r, "methods", ())
    ]


@SPECS
def test_every_operation_has_exactly_one_route(spec: OperationSpec) -> None:
    assert len(_routes(build_app(FakeApplicationService()), spec)) == 1


@SPECS
def test_the_request_body_schema_is_the_request_model(spec: OperationSpec) -> None:
    document = build_app(FakeApplicationService()).openapi()
    operation = document["paths"][PREFIX + spec.http_path][spec.http_method.lower()]
    if spec.request_model is None:
        assert "requestBody" not in operation
        return
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert _resolve(body, document["components"]["schemas"]) == _model_schema(spec.request_model)


@SPECS
def test_the_success_response_schema_is_the_result_model(spec: OperationSpec) -> None:
    document = build_app(FakeApplicationService()).openapi()
    operation = document["paths"][PREFIX + spec.http_path][spec.http_method.lower()]
    codes = [c for c in operation["responses"] if c.startswith("2")]
    assert codes
    for code in codes:
        content = operation["responses"][code].get("content")
        if content is None:  # 再送の 200 は説明だけ（本文の型は成功の code が持つ）
            continue
        schema = content["application/json"]["schema"]
        assert _resolve(schema, document["components"]["schemas"]) == _model_schema(
            spec.result_model
        )


@SPECS
async def test_the_route_forwards_caller_path_and_body_and_returns_the_result_unchanged(
    spec: OperationSpec,
) -> None:
    service = FakeApplicationService()
    async with _client(build_app(service)) as client:
        response = await _send(client, spec, headers={**AUTH, CORRELATION_HEADER: "corr-123"})
    assert response.status_code in (200, 201, 202), response.text
    [(name, args)] = service.calls
    assert name is spec.name
    caller = args[0]
    assert (caller.actor_type, caller.actor_id) == (ActorType.HUMAN, "owner")
    assert caller.correlation_id == "corr-123"
    rest = args[1:]
    if spec.request_model is not None:
        assert isinstance(rest[-1], spec.request_model)
        assert rest[-1] == spec.request_model.model_validate(BODIES[spec.name])
        rest = rest[:-1]
    if spec.name is OperationName.LIST_EPISODES:
        assert rest == (ListEpisodesRequest(),)
    elif spec.name is OperationName.CREATE_EPISODE_DRAFT:
        assert rest == ()
    elif spec.name is OperationName.LIST_TOOL_CALLS:
        assert rest == (EPISODE, ListToolCallsRequest())
    elif "{tool_call_id}" in spec.http_path:
        assert rest == (EPISODE, TOOL_CALL)
    elif "{change_set_id}" in spec.http_path:
        assert rest == (EPISODE, CHANGE_SET)
    else:
        assert rest == (EPISODE,)
    assert response.json() == results()[spec.name].model_dump(mode="json")


@SPECS
async def test_no_principal_is_401_and_unconfigured_is_503(spec: OperationSpec) -> None:
    service = FakeApplicationService()
    async with _client(build_app(service)) as client:
        assert (await _send(client, spec)).status_code == 401
        bad = await _send(client, spec, headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
    async with _client(build_app(service, auth=False)) as client:
        assert (await _send(client, spec)).status_code == 503
    assert service.calls == []


@SPECS
async def test_flag_off_is_404_feature_disabled_before_anything_else(spec: OperationSpec) -> None:
    service = FakeApplicationService()
    async with _client(build_app(service, flag=False)) as client:
        response = await _send(client, spec, headers=AUTH)
        anonymous = await _send(client, spec)
    for r in (response, anonymous):
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "feature_disabled"
    assert service.calls == []


@pytest.mark.parametrize(
    ("name", "created", "expected"),
    [
        (OperationName.CREATE_EPISODE_DRAFT, True, 201),
        (OperationName.CREATE_EPISODE_DRAFT, False, 200),
        (OperationName.START_AGENTIC_PRODUCTION, True, 202),
        (OperationName.START_AGENTIC_PRODUCTION, False, 200),
        (OperationName.REQUEST_HOLD, True, 202),
        (OperationName.REQUEST_HOLD, False, 200),
        (OperationName.RESUME_PRODUCTION, True, 202),
        (OperationName.RESUME_PRODUCTION, False, 200),
        (OperationName.PROPOSE_STORYBOARD_CHANGE, True, 202),
        (OperationName.PROPOSE_STORYBOARD_CHANGE, False, 200),
        (OperationName.APPLY_APPROVED_CHANGE, True, 202),
        (OperationName.APPLY_APPROVED_CHANGE, False, 200),
        (OperationName.LIST_EPISODES, True, 200),
        (OperationName.GET_STORYBOARD, True, 200),
        (OperationName.PREVIEW_CHANGE_IMPACT, True, 200),
    ],
)
async def test_status_is_created_or_accepted_when_new_and_200_on_resend(
    name: OperationName, created: bool, expected: int
) -> None:
    spec = next(o for o in OPERATIONS if o.name is name)
    async with _client(build_app(FakeApplicationService(created=created))) as client:
        response = await _send(client, spec, headers=AUTH)
    assert response.status_code == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (OperationErrorCode.CHANGE_SET_NOT_FOUND, 404),
        (OperationErrorCode.EPISODE_NOT_FOUND, 404),
        (OperationErrorCode.INVALID_STATE, 409),
        (OperationErrorCode.COMMAND_ID_REUSED, 409),
        (OperationErrorCode.STALE_CONTROL_REVISION, 409),
        (OperationErrorCode.STALE_TARGET_REVISION, 409),
        (OperationErrorCode.MISSING_SCOPE, 403),
        (OperationErrorCode.FORBIDDEN, 403),
        (OperationErrorCode.FEATURE_DISABLED, 404),
        (OperationErrorCode.COST_NOT_ESTIMATED, 422),
        (OperationErrorCode.BUDGET_EXCEEDED, 422),
        (OperationErrorCode.INVALID_PAYLOAD, 422),
    ],
)
@SPECS
async def test_an_operation_error_maps_to_the_same_status_and_code_on_every_route(
    spec: OperationSpec, code: OperationErrorCode, expected: int
) -> None:
    service = FakeApplicationService(raises={spec.name: OperationError(code, "msg", {"k": "v"})})
    async with _client(build_app(service)) as client:
        response = await _send(client, spec, headers=AUTH)
    assert response.status_code == expected
    assert response.json()["detail"] == {"error": code.value, "message": "msg", "k": "v"}


async def test_a_missing_correlation_header_gets_a_generated_id() -> None:
    service = FakeApplicationService()
    async with _client(build_app(service)) as client:
        await client.get(PREFIX + "/episodes", headers=AUTH)
        await client.get(PREFIX + "/episodes", headers={**AUTH, CORRELATION_HEADER: "bad id!\n"})
    first, second = (c[1][0].correlation_id for c in service.calls)
    for generated in (first, second):
        assert generated is not None and uuid.UUID(generated).version == 4
    assert first != second


async def test_list_episodes_takes_limit_and_before_as_query_and_validates_them() -> None:
    service = FakeApplicationService()
    before = datetime(2026, 10, 1, tzinfo=UTC)
    async with _client(build_app(service)) as client:
        ok = await client.get(
            PREFIX + "/episodes", params={"limit": 5, "before": before.isoformat()}, headers=AUTH
        )
        too_big = await client.get(PREFIX + "/episodes", params={"limit": 201}, headers=AUTH)
        zero = await client.get(PREFIX + "/episodes", params={"limit": 0}, headers=AUTH)
    assert ok.status_code == 200
    assert service.calls[0][1][1] == ListEpisodesRequest(limit=5, before=before)
    assert too_big.status_code == zero.status_code == 422
    assert len(service.calls) == 1


async def test_drafts_is_not_parsed_as_an_episode_id_and_existing_routes_are_untouched() -> None:
    service = FakeApplicationService()
    spec = next(o for o in OPERATIONS if o.name is OperationName.CREATE_EPISODE_DRAFT)
    async with _client(build_app(service)) as client:
        drafted = await _send(client, spec, headers=AUTH)
        # 既存の GET /episodes/{episode_id} は UUID でない値を 422 にするだけ（drafts を奪わない）
        legacy = await client.get(PREFIX + "/episodes/drafts", headers=AUTH)
    assert drafted.status_code == 201
    assert legacy.status_code == 422
    assert [c[0] for c in service.calls] == [OperationName.CREATE_EPISODE_DRAFT]


def test_the_catalog_paths_do_not_collide_with_existing_intervention_routes() -> None:
    app = build_app(FakeApplicationService())
    catalog = {(PREFIX + o.http_path, o.http_method) for o in OPERATIONS}
    seen: dict[tuple[str, str], int] = {}
    for route in _flat_routes(app):
        for method in getattr(route, "methods", ()) or ():
            key = (getattr(route, "path", ""), method)
            seen[key] = seen.get(key, 0) + 1
    assert all(seen[key] == 1 for key in catalog)


async def test_list_episodes_takes_before_id_status_and_q_as_query() -> None:
    service = FakeApplicationService()
    before = datetime(2026, 10, 1, tzinfo=UTC)
    cursor = uuid.uuid4()
    async with _client(build_app(service)) as client:
        ok = await client.get(
            PREFIX + "/episodes",
            params={
                "before": before.isoformat(),
                "before_id": str(cursor),
                "status": "planned",
                "q": "cats",
            },
            headers=AUTH,
        )
        bad_status = await client.get(PREFIX + "/episodes", params={"status": "nope"}, headers=AUTH)
        bad_id = await client.get(PREFIX + "/episodes", params={"before_id": "x"}, headers=AUTH)
    assert ok.status_code == 200
    assert service.calls[0][1][1] == ListEpisodesRequest(
        before=before, before_id=cursor, status=EpisodeStatus.PLANNED, q="cats"
    )
    assert bad_status.status_code == bad_id.status_code == 422
    assert len(service.calls) == 1


async def test_list_episodes_before_id_without_before_is_422() -> None:
    """P2-1: 複合 cursor の後半だけでは意味が無い（黙って無視すると先頭ページに戻る）。"""
    service = FakeApplicationService()
    async with _client(build_app(service)) as client:
        alone = await client.get(
            PREFIX + "/episodes", params={"before_id": str(uuid.uuid4())}, headers=AUTH
        )
        both = await client.get(
            PREFIX + "/episodes",
            params={"before_id": str(uuid.uuid4()), "before": "2026-10-01T00:00:00+00:00"},
            headers=AUTH,
        )
    assert alone.status_code == 422
    assert both.status_code == 200
    assert len(service.calls) == 1
