"""tool の実行器（ADR-0042 §3・§4）。入力を検査し、介入 HTTP API を呼び、結果を返す。

- 返り値は常に ``{"status": ...}`` を持つ dict（LLM にそのまま返せる）。
- command を送る tool の ``command_id`` は ``tool_command_id(session, tool_call)``（INV-49）。
- 権限の正本はサーバ。adapter の検査（費用の確認・状態の確認）は補助で、AI が権限を迂回する
  経路は作らない。サーバが拒否したら ``needs_human_approval`` を返し、他の資格情報で再試行しない。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.assistant.client import (
    ApiResponse,
    AssistantTransportError,
    InterventionClient,
    envelope_correlation_id,
)
from contracts.assistant_tools import TOOLS_BY_NAME, ToolName, tool_command_id
from contracts.intervention import CommandType

_COMMAND_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TERMINAL_REJECTED = "rejected"
HUMAN_APPLY_COMMAND = "python scripts/intervene.py apply {episode_id} --change-set {change_set_id}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    client: InterventionClient
    tool: ToolName
    tool_call_id: str

    @property
    def command_id(self) -> str:
        return tool_command_id(self.client.session_id, self.tool_call_id)

    def request(self, method: str, path: str, **kwargs: Any) -> ApiResponse:
        return self.client.request(
            method, path, tool=self.tool.value, tool_call_id=self.tool_call_id, **kwargs
        )


# --------------------------------------------------------------------------- 入力の検査


def validate_input(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """JSON Schema の部分集合（type / required / properties / additionalProperties / items / enum /
    minProperties）だけを検査する。残りの制約（長さ・範囲）は API の payload model が拒否する。"""
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
        return errors
    expected = schema.get("type")
    if expected is not None:
        kinds = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, k) for k in kinds):
            errors.append(f"{path}: expected {'|'.join(kinds)}")
            return errors
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        errors += [f"{path}.{k}: is required" for k in schema.get("required", []) if k not in value]
        if len(value) < schema.get("minProperties", 0):
            errors.append(f"{path}: needs at least {schema['minProperties']} field(s)")
        for key, item in value.items():
            if key in props:
                errors += validate_input(props[key], item, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unknown field")
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate_input(schema["items"], item, f"{path}[{i}]")
    return errors


def _is_type(value: Any, kind: str) -> bool:
    match kind:
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "null":
            return value is None
        case "boolean":
            return isinstance(value, bool)
    return True


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ToolInputError(f"{field} must be a UUID") from None


class ToolInputError(ValueError):
    pass


# --------------------------------------------------------------------------- 結果の形


def _ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _api_error(response: ApiResponse) -> dict[str, Any]:
    detail = response.body.get("detail") if isinstance(response.body, dict) else None
    out: dict[str, Any] = {"status": "error", "http_status": response.status}
    if isinstance(detail, dict):
        out["error"] = detail.get("error")
        out["message"] = detail.get("message")
        for key in ("required_scope", "errors", "supported_types"):
            if key in detail:
                out[key] = detail[key]
    elif isinstance(detail, list):
        out["error"] = "invalid_request"
    return out


def _result(response: ApiResponse) -> dict[str, Any]:
    return _ok(response.body) if response.ok else _api_error(response)


def _compact(mapping: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: mapping[k] for k in keys if mapping.get(k) is not None}


# --------------------------------------------------------------------------- 読み取り


def _episode_path(args: dict[str, Any]) -> str:
    return f"/episodes/{_uuid(args['episode_id'], 'episode_id')}"


def _get_episode_status(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    return _result(call.request("GET", _episode_path(args)))


def _read_decisions(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    return _result(call.request("GET", _episode_path(args) + "/decisions"))


def _explain_decision(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    decision = _uuid(args["decision_id"], "decision_id")
    return _result(call.request("GET", f"{_episode_path(args)}/decisions/{decision}/explanation"))


def _search_decisions(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    params = {k: v for k, v in args.items() if v is not None}
    if "episode_id" in params:
        params["episode_id"] = _uuid(params["episode_id"], "episode_id")
    return _result(call.request("GET", "/decisions", params=params))


def _compare_revisions(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    params = {
        "a": _uuid(args["revision_a"], "revision_a"),
        "b": _uuid(args["revision_b"], "revision_b"),
    }
    return _result(call.request("GET", _episode_path(args) + "/revisions/compare", params=params))


def _export_review_packet(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    params = {"format": args.get("format", "json")}
    return _result(call.request("GET", _episode_path(args) + "/review-packet", params=params))


def _list_change_sets(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    return _result(call.request("GET", _episode_path(args) + "/change-sets"))


def _fetch_change_set(call: ToolCall, episode_id: str, change_set_id: str) -> ApiResponse:
    return call.request("GET", f"/episodes/{episode_id}/change-sets/{change_set_id}")


def _preview_change_impact(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    episode_id = _uuid(args["episode_id"], "episode_id")
    response = _fetch_change_set(call, episode_id, _uuid(args["change_set_id"], "change_set_id"))
    if not response.ok:
        return _api_error(response)
    view = response.body
    # 未見積りは null のまま（0 と表示しない）
    return _ok(
        {
            **_compact(
                view, ("change_set_id", "kind", "status", "rationale", "proposal", "impact")
            ),
            "estimated_cost_usd": view.get("estimated_cost_usd"),
            "cost_estimated": view.get("cost_estimated"),
            "proposed_by": view.get("actor"),
        }
    )


def _get_command_status(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    command_id = str(args["command_id"])
    if not _COMMAND_ID.match(command_id):
        raise ToolInputError("command_id has an invalid format")
    response = call.request("GET", f"/commands/{command_id}")
    if not response.ok:
        return _api_error(response)
    return _command_result(call, response.body, replayed=False)


# --------------------------------------------------------------------------- command


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def build_command_payload(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """tool の入力から ``contracts.intervention`` の payload を作る。API が受け付ける項目だけ。"""
    match ToolName(tool):
        case ToolName.PROPOSE_SCRIPT_EDIT:
            change = _drop_none(
                {
                    "kind": "script.edit_scenes",
                    "base_revision_id": args["base_revision_id"],
                    "title": args.get("title"),
                    "hook": args.get("hook"),
                    "edits": args.get("edits"),
                }
            )
            return _drop_none(
                {
                    "change": change,
                    "rationale": args.get("rationale"),
                    "base_manifest_id": args.get("base_manifest_id"),
                    "variant_group": args.get("variant_group"),
                }
            )
        case ToolName.PROPOSE_SCENE_VISUAL_OVERRIDE:
            change = {
                "kind": "scene.override_visual",
                "base_revision_id": args["base_revision_id"],
                "scene_id": args["scene_id"],
                "visual_description": args["visual_description"],
            }
            return _drop_none(
                {
                    "change": change,
                    "rationale": args.get("rationale"),
                    "base_manifest_id": args.get("base_manifest_id"),
                    "variant_group": args.get("variant_group"),
                }
            )
        case ToolName.ADD_COMMENT:
            return _drop_none(
                {"anchor": args["anchor"], "body": args["body"], "kind": args.get("kind")}
            )
        case ToolName.REQUEST_HOLD:
            return _drop_none({"reason": args.get("reason")})
        case ToolName.RESUME_EPISODE:
            return _drop_none({"start_stage": args.get("start_stage")})
        case ToolName.APPLY_CHANGE:
            return _drop_none(
                {
                    "change_set_id": args["change_set_id"],
                    "max_estimated_cost_usd": args.get("acknowledged_estimated_cost_usd"),
                }
            )
    raise ToolInputError(f"{tool} does not send a command")


def _post_command(
    call: ToolCall, episode_id: str, command_type: CommandType, payload: dict[str, Any]
) -> ApiResponse:
    envelope = {
        "command_id": call.command_id,
        "type": command_type.value,
        "payload": payload,
        "correlation_id": envelope_correlation_id(call.client.session_id, call.tool_call_id),
    }
    return call.request("POST", f"/episodes/{episode_id}/commands", json=envelope)


def _command_result(call: ToolCall, view: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    """command の表示。AI の有料適用がサーバに拒否されていれば ``needs_human_approval``。"""
    if (
        view.get("type") == CommandType.APPLY_CHANGE.value
        and view.get("status") == _TERMINAL_REJECTED
        and view.get("reject_reason") == "forbidden"
    ):
        payload = view.get("payload") or {}
        return _needs_human_approval(
            call,
            str(view.get("episode_id")),
            str(payload.get("change_set_id")),
            reason="ai_may_apply_only_zero_cost_changes",
        )
    return {
        "status": "submitted",
        "replayed": replayed,
        "command_id": view.get("command_id"),
        "command_status": view.get("status"),
        "applied": bool(view.get("applied")),
        "terminal": bool(view.get("terminal")),
        "status_text": view.get("status_text"),
        "reject_reason": view.get("reject_reason"),
        "result": view.get("result"),
        "next": "Poll get_command_status with this command_id; only command_status=applied means "
        "the change was made.",
    }


def _needs_human_approval(
    call: ToolCall, episode_id: str, change_set_id: str, *, reason: str
) -> dict[str, Any]:
    """人が実行する手順を返す。この呼び出しは権限を持たないので、再試行や別の資格情報は使わない。"""
    out: dict[str, Any] = {
        "status": "needs_human_approval",
        "reason": reason,
        "episode_id": episode_id,
        "change_set_id": change_set_id,
        "impact": None,
        "estimated_cost_usd": None,
        "cost_estimated": None,
        "human_command": HUMAN_APPLY_COMMAND.format(
            episode_id=episode_id, change_set_id=change_set_id
        ),
        "message": "This apply needs a person with the apply scope. Show the impact and cost to "
        "them; do not retry.",
    }
    try:
        view = _fetch_change_set(call, episode_id, change_set_id)
    except (AssistantTransportError, ToolInputError):
        return out
    if view.ok and isinstance(view.body, dict):
        cost = view.body.get("estimated_cost_usd")
        out.update(
            impact=view.body.get("impact"),
            estimated_cost_usd=cost,
            cost_estimated=view.body.get("cost_estimated"),
        )
        if cost is not None:
            out["human_command"] += f" --max-cost {cost}"
    return out


def _send(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS_BY_NAME[call.tool.value]
    assert spec.command_type is not None
    episode_id = _uuid(args["episode_id"], "episode_id")
    response = _post_command(
        call, episode_id, spec.command_type, build_command_payload(call.tool.value, args)
    )
    if response.status == 403 and response.error == "missing_scope":
        body = response.body["detail"]
        return {
            "status": "missing_scope",
            "required_scope": body.get("required_scope"),
            "message": "this principal lacks the scope for this tool; ask a person",
        }
    if response.status == 409 and response.error == "command_id_reused":
        return {
            "status": "command_id_reused",
            "message": "this tool_call_id was already used with different input; "
            "use a new tool_call_id",
        }
    if not response.ok:
        return _api_error(response)
    return _command_result(call, response.body, replayed=response.status == 200)


def _apply_change(call: ToolCall, args: dict[str, Any]) -> dict[str, Any]:
    episode_id = _uuid(args["episode_id"], "episode_id")
    change_set_id = _uuid(args["change_set_id"], "change_set_id")
    acknowledged = args["acknowledged_estimated_cost_usd"]
    # 同じ tool 呼び出しの再試行は、保存済みの command を返す（INV-42/49）。費用の再検査より先
    existing = call.request("GET", f"/commands/{call.command_id}")
    if (
        existing.ok
        and isinstance(existing.body, dict)
        and existing.body.get("episode_id") == episode_id
    ):
        return _command_result(call, existing.body, replayed=True)
    # 送信直前に変更案を読み直す。提示した費用と違う変更を適用しない
    fetched = _fetch_change_set(call, episode_id, change_set_id)
    if not fetched.ok:
        return _api_error(fetched)
    view: dict[str, Any] = fetched.body
    if view.get("status") != "proposed":
        return {
            "status": "not_applicable",
            "change_set_status": view.get("status"),
            "message": "only a proposed change set can be applied",
        }
    current = view.get("estimated_cost_usd")
    if not _same_cost(acknowledged, current):
        return {
            "status": "cost_changed",
            "acknowledged_estimated_cost_usd": acknowledged,
            "current_estimated_cost_usd": current,
            "cost_estimated": view.get("cost_estimated"),
            "impact": view.get("impact"),
            "message": "The estimate differs from what was shown. Nothing was sent. Show the "
            "user the current estimate and call apply_change again with it.",
        }
    response = _post_command(
        call,
        episode_id,
        CommandType.APPLY_CHANGE,
        build_command_payload(ToolName.APPLY_CHANGE.value, args),
    )
    if response.status == 403 and response.error == "missing_scope":
        return _needs_human_approval(call, episode_id, change_set_id, reason="missing_scope")
    if response.status == 409 and response.error == "command_id_reused":
        return {"status": "command_id_reused", "message": "use a new tool_call_id"}
    if not response.ok:
        return _api_error(response)
    return _command_result(call, response.body, replayed=response.status == 200)


def _same_cost(acknowledged: Any, current: Any) -> bool:
    """未見積り（null）は null どうしだけ一致する。0 と null を同一視しない。"""
    if acknowledged is None or current is None:
        return acknowledged is None and current is None
    return Decimal(str(acknowledged)) == Decimal(str(current))


Handler = Callable[[ToolCall, dict[str, Any]], dict[str, Any]]

HANDLERS: dict[ToolName, Handler] = {
    ToolName.GET_EPISODE_STATUS: _get_episode_status,
    ToolName.READ_DECISIONS: _read_decisions,
    ToolName.EXPLAIN_DECISION: _explain_decision,
    ToolName.SEARCH_DECISIONS: _search_decisions,
    ToolName.COMPARE_REVISIONS: _compare_revisions,
    ToolName.EXPORT_REVIEW_PACKET: _export_review_packet,
    ToolName.LIST_CHANGE_SETS: _list_change_sets,
    ToolName.PREVIEW_CHANGE_IMPACT: _preview_change_impact,
    ToolName.PROPOSE_SCRIPT_EDIT: _send,
    ToolName.PROPOSE_SCENE_VISUAL_OVERRIDE: _send,
    ToolName.ADD_COMMENT: _send,
    ToolName.REQUEST_HOLD: _send,
    ToolName.GET_COMMAND_STATUS: _get_command_status,
    ToolName.APPLY_CHANGE: _apply_change,
    ToolName.RESUME_EPISODE: _send,
}


def execute_tool(
    client: InterventionClient, tool_name: str, arguments: Any, *, tool_call_id: str
) -> dict[str, Any]:
    """tool を1回実行する。例外を投げず、結果の dict を返す。"""
    spec = TOOLS_BY_NAME.get(tool_name)
    if spec is None:
        return {"status": "unknown_tool", "tool": tool_name}
    if not isinstance(arguments, dict):
        return {"status": "invalid_input", "errors": ["$: expected object"]}
    errors = validate_input(spec.input_schema, arguments)
    if errors:
        return {"status": "invalid_input", "errors": errors}
    call = ToolCall(client, spec.name, tool_call_id)
    try:
        return HANDLERS[spec.name](call, arguments)
    except ToolInputError as exc:
        return {"status": "invalid_input", "errors": [str(exc)]}
    except AssistantTransportError as exc:
        # 受理されたか分からない。command は同じ tool_call_id で再試行すれば二重にならない
        return {
            "status": "transport_error",
            "error": str(exc),
            "message": "No response. For command tools, retry with the SAME tool_call_id; the "
            "server returns the stored command instead of creating a second one.",
        }
