"""OpenSearch の mapping と Collector の型表が、ログ契約から生成したものと一致すること。

ADR-0040 §2。

理由は docs/testing/logging-platform-rationale.md §1。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from contracts.log_contract import (
    APP_LOG_LABEL,
    APP_LOG_LABEL_VALUE,
    KEYWORD_MAX_CHARS,
    LOG_FIELDS,
    FieldOrigin,
    FieldType,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    path = ROOT / "scripts" / "gen_log_mapping.py"
    spec = importlib.util.spec_from_file_location("gen_log_mapping", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_log_mapping"] = module
    spec.loader.exec_module(module)
    return module


def test_generated_files_are_up_to_date(gen: ModuleType) -> None:
    """契約を変えて再生成し忘れる片側更新を止める（AGENTS.md §7/§8）。"""
    stale = [
        str(path.relative_to(ROOT))
        for path, content in gen.outputs().items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    assert not stale, f"python scripts/gen_log_mapping.py で再生成すること: {stale}"


def _properties(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mappings = data["template"]["mappings"]
    assert mappings["dynamic"] is False
    return mappings["properties"]


def test_app_mapping_covers_every_contract_field() -> None:
    props = _properties(ROOT / "deploy/logging/opensearch/templates/avp-app-mappings.json")
    assert set(props) == {f.name for f in LOG_FIELDS}


@pytest.mark.parametrize("field", LOG_FIELDS, ids=lambda f: f.name)
def test_app_mapping_type_rules(field) -> None:
    """ADR-0040 §4 の写像規則。

    boolean/keyword/text に ignore_malformed を付けると template の登録が 400 になる。
    """
    props = _properties(ROOT / "deploy/logging/opensearch/templates/avp-app-mappings.json")
    m = props[field.name]
    if field.type is FieldType.OPAQUE_OBJECT:
        assert m == {"type": "object", "enabled": False}
    elif field.type is FieldType.KEYWORD:
        assert m == {"type": "keyword", "ignore_above": KEYWORD_MAX_CHARS}
    elif field.type in {FieldType.TEXT, FieldType.BOOLEAN}:
        assert m == {"type": field.type.value}
    elif field.name == "@timestamp":
        assert m == {"type": "date"}, "@timestamp は ignore_malformed にしない"
    else:
        assert m == {"type": field.type.value, "ignore_malformed": True}


def test_infra_mapping_is_a_subset_with_collector_fields() -> None:
    app = _properties(ROOT / "deploy/logging/opensearch/templates/avp-app-mappings.json")
    infra = _properties(ROOT / "deploy/logging/opensearch/templates/avp-infra-mappings.json")
    collector = {f.name for f in LOG_FIELDS if f.origin is FieldOrigin.COLLECTOR}
    assert collector <= set(infra)
    assert {"@timestamp", "message", "truncated", "redaction_applied"} <= set(infra)
    for name, m in infra.items():
        assert app[name] == m, f"{name}: app と infra で型が違う"


def test_lua_type_table_matches_contract() -> None:
    lua = (ROOT / "deploy/logging/fluent-bit/lua/contract_types.lua").read_text(encoding="utf-8")
    pairs = dict(re.findall(r'\[("[^"]+")\] = ("[a-z_]+"),', lua))
    got = {json.loads(k): json.loads(v) for k, v in pairs.items()}
    assert got == {f.name: f.type.value for f in LOG_FIELDS}
    assert f"app_label = {json.dumps(APP_LOG_LABEL)}" in lua
    assert f"app_label_value = {json.dumps(APP_LOG_LABEL_VALUE)}" in lua
    assert f"keyword_max_chars = {KEYWORD_MAX_CHARS}," in lua


def test_check_mode_reports_drift(gen: ModuleType, tmp_path, monkeypatch) -> None:
    """--check が食い違いを非0で報告する（CI で使える形であること）。"""
    target = tmp_path / "x.json"
    target.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen, "outputs", lambda: {target: "fresh"})
    assert gen.main(["--check"]) == 1
    assert target.read_text(encoding="utf-8") == "stale"
    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0
