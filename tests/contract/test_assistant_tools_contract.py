"""アシスタント tool 定義の契約（ADR-0042 §2・§4、INV-47・INV-49）。

理由は docs/testing/assistant-rationale.md §3。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import jsonschema
import pytest

from contracts.assistant_tools import (
    ASSISTANT_NAMESPACE,
    TOOL_NAMES,
    TOOLS,
    ToolEffect,
    tool_command_id,
    tool_use_definitions,
)
from contracts.intervention import (
    PAYLOAD_MODELS,
    REQUIRED_SCOPE,
    AgentRole,
    Anchor,
    CommandEnvelope,
    CommandType,
    ProposeChangePayload,
    SceneOverrideVisual,
    SceneTextEdit,
    Scope,
    ScriptEditScenes,
    TimeRange,
)

EXPECTED_TOOLS = {
    "get_episode_status",
    "read_decisions",
    "explain_decision",
    "search_decisions",
    "compare_revisions",
    "export_review_packet",
    "list_change_sets",
    "preview_change_impact",
    "propose_script_edit",
    "propose_scene_visual_override",
    "add_comment",
    "request_hold",
    "get_command_status",
    "apply_change",
    "resume_episode",
}
#: 対話 AI に許す command 型。予算・Schedule・公開設定・モード・取消・制約・checkpoint は含めない
ALLOWED_COMMAND_TYPES = {
    CommandType.COMMENT,
    CommandType.REQUEST_HOLD,
    CommandType.PROPOSE_CHANGE,
    CommandType.APPLY_CHANGE,
    CommandType.RESUME,
}
FORBIDDEN_WORDS = ("budget", "schedule", "cron", "visibility", "privacy", "publish", "public")


def _walk_property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.get("properties", {}).items():
            names.add(key)
            names |= _walk_property_names(value)
        for key, value in schema.items():
            if key != "properties":
                names |= _walk_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= _walk_property_names(item)
    return names


def _tool(name: str):
    return next(t for t in TOOLS if t.name.value == name)


def test_the_catalogue_has_exactly_the_agreed_tools() -> None:
    assert {t.name.value for t in TOOLS} == EXPECTED_TOOLS
    assert len(TOOLS) == len(EXPECTED_TOOLS) == len(TOOL_NAMES)
    for tool in TOOLS:
        assert re.fullmatch(r"[a-z0-9_]{1,64}", tool.name.value)  # 相関ヘッダとログの安全な文字
        assert tool.description.strip()


def test_every_input_schema_is_a_valid_object_schema() -> None:
    for tool in TOOLS:
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False, tool.name


def test_command_tools_derive_their_scope_from_the_single_source() -> None:
    """scope を literal で書き直さない（AGENTS.md §8）。"""
    for tool in TOOLS:
        if tool.command_type is None:
            assert tool.required_scope is Scope.READ, tool.name
            assert tool.effect is ToolEffect.READ
        else:
            assert tool.required_scope is REQUIRED_SCOPE[tool.command_type], tool.name
            assert tool.effect is not ToolEffect.READ


def test_effects_are_what_the_adr_says() -> None:
    assert _tool("add_comment").effect is ToolEffect.ANNOTATE
    assert _tool("propose_script_edit").effect is ToolEffect.PROPOSE
    assert _tool("propose_scene_visual_override").effect is ToolEffect.PROPOSE
    assert _tool("request_hold").effect is ToolEffect.CONTROL
    assert _tool("resume_episode").effect is ToolEffect.CONTROL
    assert _tool("apply_change").effect is ToolEffect.APPLY
    assert _tool("apply_change").required_scope is Scope.APPLY


def test_no_tool_changes_budget_schedule_or_visibility() -> None:
    """INV-47 と同じ検査を tool の表面にも掛ける。"""
    used = {t.command_type for t in TOOLS if t.command_type is not None}
    assert used <= ALLOWED_COMMAND_TYPES
    assert not used & {CommandType.SET_MODE, CommandType.SET_USER_CONSTRAINT}
    for tool in TOOLS:
        names = _walk_property_names(tool.input_schema) | {tool.name.value}
        for word in FORBIDDEN_WORDS:
            assert not [n for n in names if word in n.lower()], (tool.name, word)
    assert not any(t.required_scope is Scope.SET_MODE for t in TOOLS)


def test_proposal_schemas_do_not_invent_payload_fields() -> None:
    script = _tool("propose_script_edit").input_schema["properties"]
    allowed = (
        set(ScriptEditScenes.model_fields) - {"kind"}
        | set(ProposeChangePayload.model_fields) - {"change"}
        | {"episode_id", "expected_control_revision", "expected_target_revision"}
    )
    assert set(script) <= allowed
    # 台本の映像文 visual の修正はサーバが拒否する（変更が映像に出ない）。tool では露出しない
    edit_props = set(script["edits"]["items"]["properties"])
    assert edit_props <= set(SceneTextEdit.model_fields) - {"visual"}
    visual = _tool("propose_scene_visual_override").input_schema["properties"]
    assert set(visual) <= (
        set(SceneOverrideVisual.model_fields) - {"kind"}
        | set(ProposeChangePayload.model_fields) - {"change"}
        | {"episode_id", "expected_control_revision", "expected_target_revision"}
    )


def test_comment_schema_matches_the_anchor_model_and_design_section_8() -> None:
    props = _tool("add_comment").input_schema["properties"]
    anchor = props["anchor"]["properties"]
    assert set(anchor) <= set(Anchor.model_fields)
    # 設計 §8 のアンカー
    assert {"segment_id", "scene_id", "asset_id", "time_range", "manifest_id"} <= set(anchor)
    assert set(anchor["time_range"]["properties"]) == set(TimeRange.model_fields)
    assert set(props) <= {"episode_id", "anchor", "body", "kind"}


def test_apply_change_requires_the_acknowledged_cost_which_may_be_null() -> None:
    schema = _tool("apply_change").input_schema
    assert {"episode_id", "change_set_id", "acknowledged_estimated_cost_usd"} <= set(
        schema["required"]
    )
    cost = schema["properties"]["acknowledged_estimated_cost_usd"]
    validator = jsonschema.Draft202012Validator(schema)
    ok = {"episode_id": str(uuid.uuid4()), "change_set_id": str(uuid.uuid4())}
    assert not validator.is_valid(ok)  # 費用の確認なしでは適用できない
    assert validator.is_valid({**ok, "acknowledged_estimated_cost_usd": None})
    assert validator.is_valid({**ok, "acknowledged_estimated_cost_usd": 0.12})
    assert not validator.is_valid({**ok, "acknowledged_estimated_cost_usd": -1})
    assert not validator.is_valid({**ok, "acknowledged_estimated_cost_usd": "0.12"})
    assert cost["type"] == ["number", "null"]


def test_a_schema_valid_proposal_is_accepted_by_the_real_payload_model() -> None:
    """tool の schema に従った入力から作る payload が、サーバの payload model を通る。"""
    from apps.assistant.tools import build_command_payload

    cases = {
        "propose_script_edit": {
            "episode_id": str(uuid.uuid4()),
            "base_revision_id": str(uuid.uuid4()),
            "edits": [{"segment_id": "s1", "narration": "短くした"}],
            "rationale": "冗長",
        },
        "propose_scene_visual_override": {
            "episode_id": str(uuid.uuid4()),
            "base_revision_id": str(uuid.uuid4()),
            "scene_id": "sc1",
            "visual_description": "朝の縄文集落",
        },
        "add_comment": {
            "episode_id": str(uuid.uuid4()),
            "anchor": {"segment_id": "s1", "time_range": {"start_ms": 0, "end_ms": 900}},
            "body": "ここ長い",
        },
        "request_hold": {"episode_id": str(uuid.uuid4()), "reason": "確認"},
        "resume_episode": {"episode_id": str(uuid.uuid4())},
        "apply_change": {
            "episode_id": str(uuid.uuid4()),
            "change_set_id": str(uuid.uuid4()),
            "acknowledged_estimated_cost_usd": 0.5,
        },
    }
    for name, args in cases.items():
        tool = _tool(name)
        jsonschema.validate(args, tool.input_schema)
        assert tool.command_type is not None
        payload = build_command_payload(name, args)
        PAYLOAD_MODELS[tool.command_type].model_validate(payload)  # extra="forbid" を通る


def test_command_id_is_deterministic_per_tool_call() -> None:
    """INV-49: 同じ (session, tool_call) は同じ command_id、違えば違う。"""
    a = tool_command_id("sess-1", "call-1")
    assert a == tool_command_id("sess-1", "call-1")
    assert a == str(uuid.uuid5(ASSISTANT_NAMESPACE, "sess-1:call-1"))
    assert a != tool_command_id("sess-1", "call-2")
    assert a != tool_command_id("sess-2", "call-1")
    assert 8 <= len(a) <= 64  # CommandEnvelope.command_id の長さ制約
    assert str(ASSISTANT_NAMESPACE) == "6f1d5c0e-3a52-5b0b-9d0e-0a5d7d9f8c11"


def test_tool_use_definitions_are_plain_name_description_schema() -> None:
    defs = tool_use_definitions()
    assert [d["name"] for d in defs] == [t.name.value for t in TOOLS]
    for d in defs:
        assert set(d) == {"name", "description", "input_schema"}
    json.dumps(defs)  # JSON にできる


def test_the_catalogue_has_one_declaration_site() -> None:
    """tool 名の一覧を他の production module に書き直さない（AGENTS.md §8）。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for base in ("apps", "infrastructure", "workers", "domain", "scripts"):
        for path in (root / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if '"propose_scene_visual_override"' in text and path.name != "assistant_tools.py":
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_every_tool_has_a_handler(name: str) -> None:
    from apps.assistant.tools import HANDLERS

    assert name in HANDLERS


def test_expected_revision_fields_exist_on_the_envelope_and_only_on_state_changing_tools() -> None:
    envelope_fields = set(CommandEnvelope.model_fields)
    with_expected = set()
    for tool in TOOLS:
        names = set(tool.input_schema["properties"]) & {
            "expected_control_revision",
            "expected_target_revision",
        }
        if names:
            assert names <= envelope_fields
            with_expected.add(tool.name.value)
    assert with_expected == {
        "propose_script_edit",
        "propose_scene_visual_override",
        "request_hold",
        "resume_episode",
        "apply_change",
    }


def test_the_role_enum_is_derived_from_agent_role() -> None:
    role = _tool("search_decisions").input_schema["properties"]["role"]["enum"]
    assert role == [r.value for r in AgentRole]


def test_search_decisions_can_filter_by_scene() -> None:
    props = _tool("search_decisions").input_schema["properties"]
    assert props["scene_id"]["type"] == "string"


def test_resume_description_warns_about_paid_regeneration() -> None:
    text = _tool("resume_episode").description.lower()
    assert "paid regeneration" in text and "human" in text
