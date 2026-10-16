"""アシスタント adapter の振る舞い（ADR-0042 §3・§4・§6）。

理由は docs/testing/assistant-rationale.md §4。

httpx.MockTransport だけ。ネットワーク・LLM・fal・YouTube は呼ばない。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from apps.assistant.client import InterventionClient
from apps.assistant.tools import execute_tool
from contracts.assistant_tools import TOOLS_BY_NAME, tool_command_id

TOKEN = "assistant-adapter-secret-token-xyz"
EP = str(uuid.uuid4())
CS = str(uuid.uuid4())
REV = str(uuid.uuid4())
API = "/v1/intervention"


@dataclass
class FakeServer:
    """介入 API の代役。要求を記録し、route ごとに応答を返す。"""

    change_set: dict[str, Any] = field(default_factory=dict)
    command_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    post_response: Callable[[dict[str, Any]], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    fail_transport: bool = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_transport:
            raise httpx.ConnectError(f"boom {TOKEN}")
        path = request.url.path.removeprefix(API)
        if request.method == "POST":
            body = json.loads(request.content)
            if self.post_response:
                return self.post_response(body)
            return httpx.Response(202, json=_command_view(body))
        if re.fullmatch(r"/commands/[\w-]+", path):
            view = self.command_status.get(path.rsplit("/", 1)[1])
            if view is None:
                return httpx.Response(404, json={"detail": {"error": "command_not_found"}})
            return httpx.Response(200, json=view)
        if re.fullmatch(r"/episodes/[\w-]+/change-sets/[\w-]+", path):
            return httpx.Response(200, json=self.change_set)
        return httpx.Response(200, json={"path": path, "query": dict(request.url.params)})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def _command_view(body: dict[str, Any], **over: Any) -> dict[str, Any]:
    view = {
        "command_id": body["command_id"],
        "episode_id": EP,
        "type": body["type"],
        "status": "received",
        "status_text": "受付済み・未適用",
        "applied": False,
        "terminal": False,
        "payload": body["payload"],
        "reject_reason": None,
        "result": None,
    }
    view.update(over)
    return view


def _cs(cost: float | None, status: str = "proposed") -> dict[str, Any]:
    return {
        "change_set_id": CS,
        "kind": "script.edit_scenes",
        "status": status,
        "estimated_cost_usd": cost,
        "cost_estimated": cost is not None,
        "impact": {"stale": ["voice"], "regenerate": ["voice"], "reuse": ["image"]},
        "proposal": {"edits": []},
        "rationale": "短く",
        "actor": {"type": "ai_assistant", "id": "chat"},
    }


def _adapter(server: FakeServer, session: str = "sess-1"):
    client = InterventionClient(
        "http://api.test", TOKEN, session_id=session, transport=httpx.MockTransport(server)
    )

    def run(tool: str, args: dict[str, Any], tool_call_id: str = "call-1") -> dict[str, Any]:
        return execute_tool(client, tool, args, tool_call_id=tool_call_id)

    return run


def _apply_args(cost: float | None) -> dict[str, Any]:
    return {
        "episode_id": EP,
        "change_set_id": CS,
        "acknowledged_estimated_cost_usd": cost,
    }


# --------------------------------------------------------------------------- 相関・secret


def test_every_request_carries_the_correlation_and_tool_headers() -> None:
    server = FakeServer()
    run = _adapter(server, session="sess-A")
    run("get_episode_status", {"episode_id": EP}, "call-7")
    run("add_comment", {"episode_id": EP, "anchor": {"segment_id": "s1"}, "body": "x"}, "call-8")
    first, second = server.requests
    assert first.headers["X-Correlation-ID"] == "assistant:sess-A:call-7"
    assert first.headers["X-Assistant-Tool"] == "get_episode_status"
    assert second.headers["X-Correlation-ID"] == "assistant:sess-A:call-8"
    assert second.headers["X-Assistant-Tool"] == "add_comment"
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"


def test_the_token_never_appears_in_results_errors_or_repr() -> None:
    ok = FakeServer()
    broken = FakeServer(fail_transport=True)
    outputs = [
        _adapter(ok)("get_episode_status", {"episode_id": EP}),
        _adapter(broken)("get_episode_status", {"episode_id": EP}),
        _adapter(broken)(
            "add_comment", {"episode_id": EP, "anchor": {"scene_id": "a"}, "body": "x"}
        ),
        _adapter(ok)("nope", {}),
    ]
    assert outputs[1]["status"] == "transport_error"
    assert TOKEN not in json.dumps(outputs, ensure_ascii=False)
    client = InterventionClient("http://api.test", TOKEN, session_id="s")
    assert TOKEN not in repr(client)


def test_a_missing_token_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        InterventionClient("http://api.test", "", session_id="s")


# --------------------------------------------------------------------------- 入力の検査


def test_invalid_input_is_rejected_before_any_request() -> None:
    server = FakeServer()
    run = _adapter(server)
    cases = [
        ("get_episode_status", {}),  # 必須
        ("get_episode_status", {"episode_id": EP, "extra": 1}),  # 余分な項目
        ("get_episode_status", {"episode_id": "../../etc"}),  # path に入る値は UUID だけ
        ("add_comment", {"episode_id": EP, "anchor": {}, "body": "x"}),  # アンカー必須
        (
            "propose_script_edit",
            {
                "episode_id": EP,
                "base_revision_id": REV,
                "edits": [{"segment_id": "s1", "visual": "映像文"}],  # visual は露出しない
            },
        ),
        ("apply_change", {"episode_id": EP, "change_set_id": CS}),  # 費用の確認が無い
        ("get_command_status", {"command_id": "../x"}),
    ]
    for tool, args in cases:
        result = run(tool, args)
        assert result["status"] == "invalid_input", (tool, args, result)
    assert server.requests == []


def test_unknown_tool_and_non_object_arguments() -> None:
    server = FakeServer()
    assert _adapter(server)("set_budget", {"limit": 1})["status"] == "unknown_tool"
    client = InterventionClient(
        "http://api.test", TOKEN, session_id="s", transport=httpx.MockTransport(server)
    )
    assert execute_tool(client, "get_episode_status", "x", tool_call_id="c")["status"] == (
        "invalid_input"
    )
    assert server.requests == []


# --------------------------------------------------------------------------- 読み取り


@pytest.mark.parametrize(
    ("tool", "args", "path", "query"),
    [
        ("get_episode_status", {"episode_id": EP}, f"/episodes/{EP}", {}),
        ("read_decisions", {"episode_id": EP}, f"/episodes/{EP}/decisions", {}),
        (
            "explain_decision",
            {"episode_id": EP, "decision_id": CS},
            f"/episodes/{EP}/decisions/{CS}/explanation",
            {},
        ),
        (
            "search_decisions",
            {"decision_type": "script_structure", "limit": 5},
            "/decisions",
            {"decision_type": "script_structure", "limit": "5"},
        ),
        (
            "search_decisions",
            {"scene_id": "sb2"},
            "/decisions",
            {"scene_id": "sb2"},
        ),
        (
            "compare_revisions",
            {"episode_id": EP, "revision_a": REV, "revision_b": CS},
            f"/episodes/{EP}/revisions/compare",
            {"a": REV, "b": CS},
        ),
        (
            "export_review_packet",
            {"episode_id": EP, "format": "markdown"},
            f"/episodes/{EP}/review-packet",
            {"format": "markdown"},
        ),
        ("list_change_sets", {"episode_id": EP}, f"/episodes/{EP}/change-sets", {}),
    ],
)
def test_read_tools_call_the_matching_get_route(tool, args, path, query) -> None:
    server = FakeServer()
    result = _adapter(server)(tool, args)
    assert result["status"] == "ok"
    [request] = server.requests
    assert request.method == "GET"
    assert request.url.path == API + path
    assert dict(request.url.params) == query


def test_preview_keeps_an_unestimated_cost_as_null() -> None:
    server = FakeServer(change_set=_cs(None))
    result = _adapter(server)("preview_change_impact", {"episode_id": EP, "change_set_id": CS})
    assert result["data"]["estimated_cost_usd"] is None
    assert result["data"]["cost_estimated"] is False
    assert result["data"]["impact"]["regenerate"] == ["voice"]


def test_api_errors_are_returned_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"detail": {"error": "episode_not_found", "message": "gone"}}
        )

    client = InterventionClient(
        "http://api.test", TOKEN, session_id="s", transport=httpx.MockTransport(handler)
    )
    result = execute_tool(client, "get_episode_status", {"episode_id": EP}, tool_call_id="c")
    assert result == {
        "status": "error",
        "http_status": 404,
        "error": "episode_not_found",
        "message": "gone",
    }


# ------------------------------------------------------------------- command と冪等（INV-49）


def test_the_same_tool_call_sends_the_same_command_id_and_a_different_one_differs() -> None:
    server = FakeServer()
    run = _adapter(server, session="S")
    args = {"episode_id": EP, "anchor": {"segment_id": "s1"}, "body": "短く"}
    first = run("add_comment", args, "call-1")
    again = run("add_comment", args, "call-1")
    other = run("add_comment", args, "call-2")
    assert first["command_id"] == again["command_id"] == tool_command_id("S", "call-1")
    assert other["command_id"] == tool_command_id("S", "call-2") != first["command_id"]
    bodies = [json.loads(r.content) for r in server.posts]
    assert [b["command_id"] for b in bodies] == [
        tool_command_id("S", "call-1"),
        tool_command_id("S", "call-1"),
        tool_command_id("S", "call-2"),
    ]
    assert all(len(b["correlation_id"]) <= 64 for b in bodies)


def test_a_replayed_command_is_marked_replayed() -> None:
    server = FakeServer(post_response=lambda body: httpx.Response(200, json=_command_view(body)))
    result = _adapter(server)("request_hold", {"episode_id": EP, "reason": "確認"}, "c1")
    assert result["status"] == "submitted" and result["replayed"] is True
    assert result["applied"] is False  # received を完了と表示しない


def test_command_tools_send_the_right_type_and_only_accepted_payload_fields() -> None:
    server = FakeServer()
    run = _adapter(server)
    run(
        "propose_script_edit",
        {
            "episode_id": EP,
            "base_revision_id": REV,
            "edits": [{"segment_id": "s1", "narration": "新"}],
            "rationale": "冗長",
        },
        "p1",
    )
    run(
        "propose_scene_visual_override",
        {"episode_id": EP, "base_revision_id": REV, "scene_id": "sc1", "visual_description": "朝"},
        "p2",
    )
    run("resume_episode", {"episode_id": EP}, "r1")
    bodies = [json.loads(r.content) for r in server.posts]
    assert [b["type"] for b in bodies] == ["propose_change", "propose_change", "resume"]
    assert bodies[0]["payload"]["change"]["kind"] == "script.edit_scenes"
    assert bodies[1]["payload"]["change"]["kind"] == "scene.override_visual"
    assert bodies[2]["payload"] == {}
    for r in server.posts:
        assert r.url.path == f"{API}/episodes/{EP}/commands"


def test_a_reused_tool_call_id_with_different_input_is_reported() -> None:
    server = FakeServer(
        post_response=lambda body: httpx.Response(
            409, json={"detail": {"error": "command_id_reused", "message": "m"}}
        )
    )
    result = _adapter(server)("request_hold", {"episode_id": EP}, "c1")
    assert result["status"] == "command_id_reused"


def test_a_missing_scope_on_a_non_apply_tool_is_reported_without_a_human_command() -> None:
    server = FakeServer(
        post_response=lambda body: httpx.Response(
            403, json={"detail": {"error": "missing_scope", "required_scope": "hold"}}
        )
    )
    result = _adapter(server)("request_hold", {"episode_id": EP}, "c1")
    assert result == {
        "status": "missing_scope",
        "required_scope": "hold",
        "message": "this principal lacks the scope for this tool; ask a person",
    }
    assert len(server.posts) == 1


def test_no_response_asks_for_a_retry_with_the_same_tool_call_id() -> None:
    server = FakeServer(fail_transport=True)
    result = _adapter(server)("request_hold", {"episode_id": EP}, "c1")
    assert result["status"] == "transport_error"
    assert "SAME tool_call_id" in result["message"]


# --------------------------------------------------------------------------- apply_change


def test_apply_with_the_acknowledged_cost_sends_one_apply_command() -> None:
    server = FakeServer(change_set=_cs(0.5))
    result = _adapter(server, "S")("apply_change", _apply_args(0.5), "a1")
    assert result["status"] == "submitted"
    [post] = server.posts
    body = json.loads(post.content)
    assert body["type"] == "apply_change"
    assert body["command_id"] == tool_command_id("S", "a1")
    assert body["payload"] == {"change_set_id": CS, "max_estimated_cost_usd": 0.5}
    # 送信直前に変更案を読み直している（POST より前の GET）
    gets = [r.url.path for r in server.requests[: server.requests.index(post)]]
    assert f"{API}/episodes/{EP}/change-sets/{CS}" in gets


def test_a_changed_estimate_sends_nothing_and_returns_cost_changed() -> None:
    server = FakeServer(change_set=_cs(0.8))
    result = _adapter(server)("apply_change", _apply_args(0.5))
    assert result["status"] == "cost_changed"
    assert result["acknowledged_estimated_cost_usd"] == 0.5
    assert result["current_estimated_cost_usd"] == 0.8
    assert result["impact"]["regenerate"] == ["voice"]
    assert server.posts == []  # command を送っていない


@pytest.mark.parametrize(
    ("server_cost", "acknowledged"),
    [(None, 0.0), (None, 0.5), (0.5, None), (0.0, None), (0.0, 0.5)],
)
def test_null_and_zero_are_never_confused(server_cost, acknowledged) -> None:
    server = FakeServer(change_set=_cs(server_cost))
    result = _adapter(server)("apply_change", _apply_args(acknowledged))
    assert result["status"] == "cost_changed"
    assert server.posts == []


def test_an_unestimated_change_can_be_applied_only_with_an_explicit_null() -> None:
    server = FakeServer(change_set=_cs(None))
    result = _adapter(server)("apply_change", _apply_args(None))
    assert result["status"] == "submitted"
    [post] = server.posts
    assert json.loads(post.content)["payload"] == {"change_set_id": CS}  # 上限を作らない


def test_a_zero_estimate_acknowledged_as_zero_is_sent_with_a_zero_cap() -> None:
    server = FakeServer(change_set=_cs(0.0))
    assert _adapter(server)("apply_change", _apply_args(0.0))["status"] == "submitted"
    assert json.loads(server.posts[0].content)["payload"]["max_estimated_cost_usd"] == 0.0


def test_a_change_set_that_is_not_proposed_is_not_applied() -> None:
    server = FakeServer(change_set=_cs(0.0, status="applied"))
    result = _adapter(server)("apply_change", _apply_args(0.0))
    assert result["status"] == "not_applicable" and result["change_set_status"] == "applied"
    assert server.posts == []


def test_a_forbidden_scope_returns_needs_human_approval_and_does_not_retry() -> None:
    server = FakeServer(
        change_set=_cs(0.5),
        post_response=lambda body: httpx.Response(
            403, json={"detail": {"error": "missing_scope", "required_scope": "apply"}}
        ),
    )
    result = _adapter(server)("apply_change", _apply_args(0.5))
    assert result["status"] == "needs_human_approval"
    assert result["impact"]["regenerate"] == ["voice"]
    assert result["estimated_cost_usd"] == 0.5
    assert result["human_command"] == (
        f"python scripts/intervene.py apply {EP} --change-set {CS} --max-cost 0.5"
    )
    assert len(server.posts) == 1  # 再試行しない
    assert TOKEN not in json.dumps(result)


def test_an_ai_paid_apply_rejected_by_the_server_becomes_needs_human_approval() -> None:
    def reject(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json=_command_view(body, status="rejected", terminal=True, reject_reason="forbidden"),
        )

    server = FakeServer(change_set=_cs(0.5), post_response=reject)
    result = _adapter(server)("apply_change", _apply_args(0.5))
    assert result["status"] == "needs_human_approval"
    assert result["reason"] == "forbidden"  # サーバの理由をそのまま（relabel しない）
    assert result["estimated_cost_usd"] == 0.5


def test_get_command_status_maps_a_forbidden_apply_the_same_way() -> None:
    cid = tool_command_id("sess-1", "a9")
    view = _command_view(
        {"command_id": cid, "type": "apply_change", "payload": {"change_set_id": CS}},
        status="rejected",
        terminal=True,
        reject_reason="forbidden",
    )
    server = FakeServer(change_set=_cs(None), command_status={cid: view})
    result = _adapter(server)("get_command_status", {"command_id": cid})
    assert result["status"] == "needs_human_approval"
    assert result["estimated_cost_usd"] is None  # 未見積りを 0 にしない
    assert result["human_command"] == f"python scripts/intervene.py apply {EP} --change-set {CS}"


def test_get_command_status_never_calls_a_non_applied_command_applied() -> None:
    cid = "cmd-received-001"
    view = _command_view({"command_id": cid, "type": "comment", "payload": {}}, status="applying")
    server = FakeServer(command_status={cid: view})
    result = _adapter(server)("get_command_status", {"command_id": cid})
    assert result["command_status"] == "applying" and result["applied"] is False


def test_a_retried_apply_returns_the_stored_command_without_rechecking_cost() -> None:
    """INV-49: 適用後に変更案が applied でも、同じ tool 呼び出しは保存済みの command を返す。"""
    cid = tool_command_id("sess-1", "a1")
    stored = _command_view(
        {"command_id": cid, "type": "apply_change", "payload": {"change_set_id": CS}},
        status="applied",
        applied=True,
        terminal=True,
    )
    server = FakeServer(change_set=_cs(0.5, status="applied"), command_status={cid: stored})
    result = _adapter(server)("apply_change", _apply_args(0.5), "a1")
    assert result["status"] == "submitted" and result["replayed"] is True
    assert result["applied"] is True
    assert server.posts == []


def test_every_tool_in_the_catalogue_can_be_dispatched() -> None:
    """カタログに足して実行器を足し忘れない（未実装の tool を LLM に見せない）。"""
    from apps.assistant.tools import HANDLERS

    assert {n.value for n in HANDLERS} == set(TOOLS_BY_NAME)


# --------------------------------------------------------------------------- reviewer E2


def _stored(cid: str, type_: str, payload: dict[str, Any], **over: Any) -> dict[str, Any]:
    return _command_view(
        {"command_id": cid, "type": type_, "payload": payload},
        status="applied",
        applied=True,
        terminal=True,
        **over,
    )


def test_a_tool_call_id_used_for_another_command_is_command_id_reused_not_applied() -> None:
    """M2: 同じ tool_call_id が propose に使われていたら、apply の結果として返さない。"""
    cid = tool_command_id("sess-1", "t1")
    server = FakeServer(
        change_set=_cs(0.5),
        command_status={cid: _stored(cid, "propose_change", {"change": {}})},
    )
    result = _adapter(server)("apply_change", _apply_args(0.5), "t1")
    assert result["status"] == "command_id_reused"
    assert server.posts == []


def test_a_stored_apply_for_another_change_set_or_episode_is_command_id_reused() -> None:
    cid = tool_command_id("sess-1", "t2")
    other = str(uuid.uuid4())
    for stored in (
        _stored(cid, "apply_change", {"change_set_id": other}),
        _stored(cid, "apply_change", {"change_set_id": CS}, episode_id=other),
    ):
        server = FakeServer(change_set=_cs(0.5), command_status={cid: stored})
        assert _adapter(server)("apply_change", _apply_args(0.5), "t2")["status"] == (
            "command_id_reused"
        )
        assert server.posts == []


def test_human_command_is_built_only_from_strict_uuids_and_quoted() -> None:
    """M3: 保存された command の id をそのままシェル文字列に入れない。"""
    evil = "x; touch /tmp/pwned #"
    cid = "cmd-evil-00001"
    view = _command_view(
        {"command_id": cid, "type": "apply_change", "payload": {"change_set_id": evil}},
        status="rejected",
        terminal=True,
        reject_reason="forbidden",
    )
    server = FakeServer(change_set=_cs(0.5), command_status={cid: view})
    result = _adapter(server)("get_command_status", {"command_id": cid})
    assert result["status"] == "needs_human_approval"
    assert "human_command" not in result
    assert "touch" not in json.dumps(result)
    assert all("pwned" not in str(r.url) for r in server.requests)
    # 正常な UUID なら shlex.quote を通した形（UUID は quote されても同じ）
    ok = _adapter(FakeServer(change_set=_cs(0.5)))("apply_change", _apply_args(0.5))
    assert ok["status"] == "submitted"


def test_resume_forbidden_for_paid_generation_needs_a_human() -> None:
    def reject(body: dict[str, Any]) -> httpx.Response:
        view = _command_view(
            body,
            status="rejected",
            terminal=True,
            reject_reason="forbidden",
            result={"reason": "forbidden", "detail": "ai_resume_requires_paid_generation"},
        )
        return httpx.Response(200, json=view)

    server = FakeServer(post_response=reject)
    result = _adapter(server)("resume_episode", {"episode_id": EP}, "r1")
    assert result["status"] == "needs_human_approval"
    assert result["server_detail"] == "ai_resume_requires_paid_generation"
    assert result["reason"] == "ai_resume_requires_paid_generation"
    assert result["human_command"] == f"python scripts/intervene.py resume {EP}"
    assert len(server.posts) == 1


def test_forbidden_reason_is_surfaced_verbatim_not_relabelled() -> None:
    def reject(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json=_command_view(
                body,
                status="rejected",
                terminal=True,
                reject_reason="forbidden",
                result={"reason": "forbidden", "detail": "paid_apply_requires_human"},
            ),
        )

    server = FakeServer(change_set=_cs(0.5), post_response=reject)
    result = _adapter(server)("apply_change", _apply_args(0.5))
    assert result["status"] == "needs_human_approval"
    assert result["reason"] == "paid_apply_requires_human"
    assert result["server_reason"] == "forbidden"


def test_expected_revisions_are_passed_through_and_control_revision_is_echoed() -> None:
    server = FakeServer(change_set=_cs(0.0))
    run = _adapter(server)
    run("request_hold", {"episode_id": EP, "expected_control_revision": 4}, "h1")
    run(
        "propose_script_edit",
        {"episode_id": EP, "base_revision_id": REV, "expected_target_revision": REV},
        "p1",
    )
    run("resume_episode", {"episode_id": EP, "expected_control_revision": 5}, "r1")
    args = _apply_args(0.0) | {"expected_control_revision": 6}
    run("apply_change", args, "a1")
    bodies = [json.loads(r.content) for r in server.posts]
    assert [b.get("expected_control_revision") for b in bodies] == [4, None, 5, 6]
    assert bodies[1]["expected_target_revision"] == REV

    def status_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"control": {"control_revision": 9}})

    client = InterventionClient(
        "http://api.test", TOKEN, session_id="s", transport=httpx.MockTransport(status_handler)
    )
    out = execute_tool(client, "get_episode_status", {"episode_id": EP}, tool_call_id="c")
    assert out["control_revision"] == 9
    assert "expected_control_revision" in out["hint"]
