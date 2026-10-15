"""契約同期（INV-52、ADR-0043 §1）。理由は docs/testing/jpmas2-p0b-rationale.md。

OpenAPI の route・アシスタント tool の入力 schema・``contracts/application.py`` の model が一致し、
固定した OpenAPI 断片が生成結果と同じであること。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

from apps.api.main import create_app
from contracts.application import (
    ProposeStoryboardChangeRequest,
    StoryboardPatchProposal,
    StoryboardProposalResult,
)
from contracts.application_operations import OPERATIONS
from contracts.assistant_tools import TOOLS_BY_NAME

REPO = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "docs" / "api" / "openapi.application.json"
ROUTE = "/v1/intervention/episodes/{episode_id}/storyboard/proposals"


def _export_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "export_openapi", REPO / "scripts/export-openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    """``$ref`` を中身で置き換える（OpenAPI の components と model の $defs のどちらでも）。"""
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            return _resolve({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
        # FastAPI は OpenAPI から ``"default": null`` を落とす。意味は同じなので両側で落として比べる
        return {
            k: _resolve(v, defs)
            for k, v in node.items()
            if k != "$defs" and not (k == "default" and v is None)
        }
    if isinstance(node, list):
        return [_resolve(v, defs) for v in node]
    return node


def _model_schema(model: Any) -> dict[str, Any]:
    raw = model.model_json_schema()
    return _resolve(raw, raw.get("$defs", {}))


def _operation() -> tuple[dict[str, Any], dict[str, Any]]:
    document = create_app().openapi()
    return document["paths"][ROUTE]["post"], document["components"]["schemas"]


def test_the_route_request_schema_is_the_request_model() -> None:
    operation, components = _operation()
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert _resolve(body, components) == _model_schema(ProposeStoryboardChangeRequest)


def test_the_route_response_schema_is_the_result_model() -> None:
    operation, components = _operation()
    for code in ("202",):
        schema = operation["responses"][code]["content"]["application/json"]["schema"]
        assert _resolve(schema, components) == _model_schema(StoryboardProposalResult)
    assert "200" in operation["responses"]  # 再送


def test_the_tool_input_schema_is_derived_from_the_proposal_model() -> None:
    tool = _resolve(TOOLS_BY_NAME["propose_storyboard_patch"].input_schema, {})
    proposal = _model_schema(StoryboardPatchProposal)
    assert set(tool["properties"]) == set(proposal["properties"]) | {"expected_control_revision"}
    for name, schema in proposal["properties"].items():
        assert tool["properties"][name] == schema, name
    assert tool["required"] == proposal["required"]
    assert tool["additionalProperties"] is False
    assert "command_id" not in tool["properties"]  # tool 呼び出しから決まる（INV-49）


def test_the_committed_openapi_fragment_equals_a_fresh_generation() -> None:
    export = _export_module()
    fresh = export.render(export.application_openapi())
    assert SNAPSHOT.read_text(encoding="utf-8") == fresh, (
        "run: python scripts/export-openapi.py --application"
    )


def test_the_fragment_has_only_the_application_paths_and_the_referenced_schemas() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    # ADR-0044 §4: 断片は OPERATIONS の 13 操作の path 全部（P0-B の時点では提案の 1 本だった）
    expected = {"/v1/intervention" + o.http_path for o in OPERATIONS}
    assert set(document["paths"]) == expected
    assert ROUTE in document["paths"]
    names = set(document["components"]["schemas"])
    assert {"ProposeStoryboardChangeRequest", "StoryboardProposalResult"} <= names
    assert "CommandView" not in names  # 介入 API の他の route の型は含めない
