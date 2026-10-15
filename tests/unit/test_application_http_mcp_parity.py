"""MCP 風の adapter が HTTP と同じ service を使える証明（ADR-0044 §2）。

理由は docs/testing/jpmas2-p2a-http-rationale.md。MCP server は作らない: ``OperationName`` で
service のメソッドを引くだけの test 専用 adapter と HTTP route が、同じ結果・同じ error code
を返す。
"""

from __future__ import annotations

import httpx
import pytest

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller
from contracts.application_operations import OPERATIONS, OperationSpec
from contracts.intervention import ActorType, Scope
from tests.support.application_http import (
    AUTH,
    BODIES,
    CHANGE_SET,
    EPISODE,
    FakeApplicationService,
    build_app,
    results,
    url,
)

IDS = [o.name.value for o in OPERATIONS]


class FakeMcpAdapter:
    """tool 名 = 操作名。引数を service の引数へ並べ替えて同じメソッドを呼ぶだけ。"""

    def __init__(self, service: FakeApplicationService) -> None:
        self._service = service
        self._caller = Caller(ActorType.HUMAN, "owner", frozenset(Scope), correlation_id="mcp-1")

    async def call(self, spec: OperationSpec) -> dict[str, object]:
        args: list[object] = []
        if "{episode_id}" in spec.http_path:
            args.append(EPISODE)
        if "{change_set_id}" in spec.http_path:
            args.append(CHANGE_SET)
        if spec.request_model is not None:
            args.append(spec.request_model.model_validate(BODIES[spec.name]))
        elif spec.name.value == "list_episodes":
            from contracts.application_operations import ListEpisodesRequest

            args.append(ListEpisodesRequest())
        try:
            result = await getattr(self._service, spec.name.value)(self._caller, *args)
        except OperationError as exc:
            return {"error": exc.code.value}
        return result.model_dump(mode="json")  # type: ignore[no-any-return]


@pytest.mark.parametrize("spec", OPERATIONS, ids=IDS)
async def test_http_and_an_mcp_style_adapter_get_the_same_result(spec: OperationSpec) -> None:
    service = FakeApplicationService()
    via_mcp = await FakeMcpAdapter(service).call(spec)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(service)), base_url="http://t"
    ) as client:
        http = await client.request(
            spec.http_method, url(spec), json=BODIES.get(spec.name), headers=AUTH
        )
    assert http.json() == via_mcp == results()[spec.name].model_dump(mode="json")
    # 同じ service が 2 回呼ばれ、同じ引数（caller の相関 ID を除く）を受け取った
    (n1, a1), (n2, a2) = service.calls
    assert n1 is n2 is spec.name
    assert a1[1:] == a2[1:]


@pytest.mark.parametrize("spec", OPERATIONS, ids=IDS)
async def test_http_and_an_mcp_style_adapter_get_the_same_error_code(spec: OperationSpec) -> None:
    error = OperationError(OperationErrorCode.INVALID_STATE, "not now")
    service = FakeApplicationService(raises={spec.name: error})
    via_mcp = await FakeMcpAdapter(service).call(spec)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(service)), base_url="http://t"
    ) as client:
        http = await client.request(
            spec.http_method, url(spec), json=BODIES.get(spec.name), headers=AUTH
        )
    assert via_mcp == {"error": "invalid_state"}
    assert http.json()["detail"]["error"] == via_mcp["error"]
