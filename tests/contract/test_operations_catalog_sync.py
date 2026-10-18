"""``apps/web/static/operations.json`` は OPERATIONS から生成したものと同じ（ADR-0045 §2、INV-52）。

理由は docs/testing/jpmas2-p2b-rationale.md。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

from contracts.application_operations import OPERATIONS

REPO = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "apps" / "web" / "static" / "operations.json"


def _export() -> Any:
    spec = importlib.util.spec_from_file_location(
        "export_operations", REPO / "scripts/export-operations.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_catalog_equals_a_fresh_generation() -> None:
    export = _export()
    assert SNAPSHOT.read_text(encoding="utf-8") == export.render(export.build()), (
        "run: python scripts/export-operations.py"
    )


def test_the_names_match_operations_exactly() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    names = [o["name"] for o in document["operations"]]
    assert names == [o.name.value for o in OPERATIONS]
    assert len(names) == len(set(names)) == 16


def test_each_entry_carries_the_catalog_facts_and_nothing_invented() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for entry, spec in zip(document["operations"], OPERATIONS, strict=True):
        assert entry["method"] == spec.http_method
        assert entry["path"] == "/v1/intervention" + spec.http_path
        assert entry["required_scope"] == spec.required_scope.value
        assert entry["human_only"] is spec.human_only
        assert entry["paid"] is spec.paid
        assert entry["effect"] == spec.effect.value
        assert set(entry) == {
            "name",
            "method",
            "path",
            "path_params",
            "required_scope",
            "human_only",
            "paid",
            "effect",
            "body_fields",
            "query_fields",
        }


def test_fields_come_from_the_request_models() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    by_name = {o["name"]: o for o in document["operations"]}
    apply = {f["name"]: f for f in by_name["apply_approved_change"]["body_fields"]}
    assert apply["approved_max_cost_usd"]["required"] is True
    assert apply["command_id"]["required"] is True
    assert apply["expected_control_revision"]["required"] is False
    assert apply["expected_control_revision"]["nullable"] is True
    listing = {f["name"]: f for f in by_name["list_episodes"]["query_fields"]}
    assert set(listing) == {"limit", "before", "before_id", "status", "q"}
    assert "planned" in listing["status"]["enum"]
    assert by_name["get_episode_workspace"]["path_params"] == ["episode_id"]
    assert by_name["preview_change_impact"]["path_params"] == ["episode_id", "change_set_id"]


def test_catalog_version_is_stable_and_changes_with_the_content() -> None:
    export = _export()
    first, second = export.build(), export.build()
    assert first["catalog_version"] == second["catalog_version"]
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert document["catalog_version"] == first["catalog_version"]
    changed = json.loads(json.dumps(first["operations"]))
    changed[0]["human_only"] = not changed[0]["human_only"]
    import hashlib

    other = hashlib.sha256(
        json.dumps(changed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    assert other != first["catalog_version"]


def test_the_paid_operation_set_is_pinned() -> None:
    """P1-2: 有料の外部生成を伴う操作。変えるなら ADR-0044/0045 と UI の警告を見直す。

    resume_production は ADR-0044 の通り固定計画を歩き、未完了 stage を再生成しうるため有料。
    """
    paid = {o.name.value for o in OPERATIONS if o.paid}
    assert paid == {"start_agentic_production", "resume_production", "apply_approved_change"}
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert {o["name"] for o in document["operations"] if o["paid"]} == paid
    # 読み取り・提案・hold は有料になり得ない
    for spec in OPERATIONS:
        if spec.paid:
            assert spec.effect.value in ("control", "apply"), spec.name
