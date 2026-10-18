"""tool 可視化 3 操作の service と HTTP（ADR-0047 §7）。理由: agent-tools-rationale.md"""

from __future__ import annotations

import uuid

import httpx
import pytest

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, OperationRecord
from application.service import ApplicationService
from contracts.application_operations import (
    OPERATIONS_BY_NAME,
    ListToolCallsRequest,
    OperationName,
)
from contracts.intervention import ActorType, Scope
from tests.support.application_http import (
    AUTH,
    EPISODE,
    TOOL_CALL,
    FakeApplicationService,
    build_app,
    results,
)

TOOL_OPERATIONS = (
    OperationName.LIST_TOOL_CALLS,
    OperationName.GET_TOOL_CALL,
    OperationName.GET_IMPROVEMENT_TRACE,
)


class Observer:
    def __init__(self) -> None:
        self.records: list[OperationRecord] = []

    def record(self, record: OperationRecord) -> None:
        self.records.append(record)


class Evidence:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_tool_calls(self, episode_id, request):
        self.calls.append("list")
        return results()[OperationName.LIST_TOOL_CALLS]

    async def get_tool_call(self, episode_id, tool_call_id):
        self.calls.append("get")
        return results()[OperationName.GET_TOOL_CALL]

    async def improvement_trace(self, episode_id):
        self.calls.append("trace")
        return results()[OperationName.GET_IMPROVEMENT_TRACE]


class _Unused:
    def __getattr__(self, name: str):  # 読み取り 3 操作は他の port に触れない
        raise AssertionError(f"unexpected use of {name}")


def service(evidence: Evidence | None, observer: Observer) -> ApplicationService:
    return ApplicationService(
        commands=_Unused(),  # type: ignore[arg-type]
        reads=_Unused(),  # type: ignore[arg-type]
        drafts=_Unused(),  # type: ignore[arg-type]
        starter=_Unused(),  # type: ignore[arg-type]
        agentic_enabled=True,
        observer=observer,
        tool_evidence=evidence,  # type: ignore[arg-type]
    )


async def _call(svc: ApplicationService, name: OperationName, caller: Caller):
    match name:
        case OperationName.LIST_TOOL_CALLS:
            return await svc.list_tool_calls(caller, EPISODE, ListToolCallsRequest())
        case OperationName.GET_TOOL_CALL:
            return await svc.get_tool_call(caller, EPISODE, TOOL_CALL)
        case _:
            return await svc.get_improvement_trace(caller, EPISODE)


def caller(*scopes: Scope, actor: ActorType = ActorType.HUMAN) -> Caller:
    return Caller(actor, "x", frozenset(scopes), correlation_id="corr-1")


def test_the_three_operations_are_read_only_read_scope_and_flag_gated() -> None:
    for name in TOOL_OPERATIONS:
        spec = OPERATIONS_BY_NAME[name]
        assert spec.effect.value == "read" and spec.required_scope is Scope.READ
        assert spec.http_method == "GET" and not spec.paid and not spec.human_only
        assert spec.feature_flag == "jpmas2_agent_tools"


@pytest.mark.parametrize("name", TOOL_OPERATIONS)
async def test_read_scope_is_required_and_the_port_is_not_touched_without_it(
    name: OperationName,
) -> None:
    evidence, observer = Evidence(), Observer()
    with pytest.raises(OperationError) as err:
        await _call(service(evidence, observer), name, caller(Scope.COMMENT))
    assert err.value.code is OperationErrorCode.MISSING_SCOPE
    assert evidence.calls == []


@pytest.mark.parametrize("name", TOOL_OPERATIONS)
async def test_without_the_port_the_operation_is_feature_disabled(name: OperationName) -> None:
    observer = Observer()
    with pytest.raises(OperationError) as err:
        await _call(service(None, observer), name, caller(Scope.READ))
    assert err.value.code is OperationErrorCode.FEATURE_DISABLED


@pytest.mark.parametrize("name", TOOL_OPERATIONS)
async def test_an_ai_caller_with_read_may_read_and_one_observation_is_recorded(
    name: OperationName,
) -> None:
    """既存の application.operation.completed の経路（_observed）を通る。入力全文は載らない。"""
    evidence, observer = Evidence(), Observer()
    await _call(service(evidence, observer), name, caller(Scope.READ, actor=ActorType.AGENT))
    [rec] = observer.records
    assert rec.operation is name and rec.result == "succeeded"
    assert rec.episode_id == str(EPISODE) and rec.actor_type is ActorType.AGENT
    assert not hasattr(rec, "input") and len(evidence.calls) == 1


async def test_a_port_error_is_recorded_as_rejected_with_its_code() -> None:
    observer = Observer()

    class Failing(Evidence):
        async def get_tool_call(self, episode_id, tool_call_id):
            raise OperationError(OperationErrorCode.INTEGRITY_ERROR, "sha256 mismatch")

    with pytest.raises(OperationError):
        await service(Failing(), observer).get_tool_call(caller(Scope.READ), EPISODE, TOOL_CALL)
    [rec] = observer.records
    assert (rec.result, rec.error_code) == ("rejected", "integrity_error")


# --------------------------------------------------------------------------- HTTP


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _urls() -> list[str]:
    base = f"/v1/intervention/episodes/{EPISODE}"
    return [f"{base}/tool-calls", f"{base}/tool-calls/{TOOL_CALL}", f"{base}/improvement-trace"]


@pytest.mark.parametrize("path", _urls())
async def test_with_the_flag_off_every_route_is_404_feature_disabled_before_auth(
    path: str,
) -> None:
    fake = FakeApplicationService()
    async with _client(build_app(fake, agent_tools=False)) as client:
        for headers in ({}, AUTH):
            response = await client.get(path, headers=headers)
            assert response.status_code == 404
            assert response.json()["detail"]["error"] == "feature_disabled"
    assert fake.calls == []


@pytest.mark.parametrize("path", _urls())
async def test_with_the_application_api_off_the_routes_are_404_too(path: str) -> None:
    async with _client(build_app(FakeApplicationService(), flag=False)) as client:
        response = await client.get(path, headers=AUTH)
    assert response.status_code == 404 and response.json()["detail"]["error"] == "feature_disabled"


@pytest.mark.parametrize("path", _urls())
async def test_with_the_flag_on_auth_is_still_required(path: str) -> None:
    fake = FakeApplicationService()
    async with _client(build_app(fake)) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=AUTH)).status_code == 200
    assert len(fake.calls) == 1


async def test_error_codes_map_to_404_and_500_not_422() -> None:
    fake = FakeApplicationService(
        raises={
            OperationName.GET_TOOL_CALL: OperationError(
                OperationErrorCode.TOOL_CALL_NOT_FOUND, "no such call"
            ),
            OperationName.GET_IMPROVEMENT_TRACE: OperationError(
                OperationErrorCode.INTEGRITY_ERROR, "sha256 mismatch", {"reason": "sha256_mismatch"}
            ),
        }
    )
    async with _client(build_app(fake)) as client:
        missing = await client.get(_urls()[1], headers=AUTH)
        broken = await client.get(_urls()[2], headers=AUTH)
    assert missing.status_code == 404 and missing.json()["detail"]["error"] == "tool_call_not_found"
    assert broken.status_code == 500
    assert broken.json()["detail"]["reason"] == "sha256_mismatch"


async def test_query_parameters_reach_the_service_as_the_request_model() -> None:
    fake = FakeApplicationService()
    async with _client(build_app(fake)) as client:
        response = await client.get(
            _urls()[0],
            params={"agent_role": "strategy", "tool_id": "search_trends", "limit": 5},
            headers=AUTH,
        )
        bad = await client.get(_urls()[0], params={"tool_id": "not_a_tool"}, headers=AUTH)
        half = await client.get(_urls()[0], params={"before_id": "abc"}, headers=AUTH)
    assert response.status_code == 200
    (_, args) = fake.calls[0]
    assert args[2].agent_role.value == "strategy" and args[2].limit == 5
    assert bad.status_code == 422 and half.status_code == 422
    assert uuid.UUID(TOOL_CALL)
