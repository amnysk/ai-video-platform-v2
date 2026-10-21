"""deploy/logging の異常クエリ・アラート定義は「定義だけ」で、読み取り DSL に限られる。"""

from __future__ import annotations

import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
QUERIES = REPO / "deploy" / "logging" / "queries" / "daily-anomaly-queries.json"
ALERTS = REPO / "deploy" / "logging" / "alerts" / "daily-anomaly-monitors.json"
EMISSION = (REPO / "docs" / "observability" / "emission-points.md").read_text(encoding="utf-8")
EVENT = re.compile(r"\b[a-z_]+(?:\.[a-z_]+)+\b")


def _events(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "event_name":
                found |= set(v if isinstance(v, list) else [v])
            else:
                found |= _events(v)
    elif isinstance(node, list):
        for v in node:
            found |= _events(v)
    return found


def test_definitions_are_marked_not_applied_and_monitors_disabled_without_actions() -> None:
    q = json.loads(QUERIES.read_text(encoding="utf-8"))
    a = json.loads(ALERTS.read_text(encoding="utf-8"))
    assert q["_meta"]["applied"] is False and a["_meta"]["applied"] is False
    for m in a["monitors"]:
        assert m["enabled"] is False, m["name"]
        assert all(t["actions"] == [] for t in m["triggers"]), m["name"]


def test_every_query_is_a_size_zero_search_with_no_script_or_write_clause() -> None:
    q = json.loads(QUERIES.read_text(encoding="utf-8"))["queries"]
    assert q
    for name, body in q.items():
        assert body["size"] == 0, name
        assert "script" not in json.dumps(body), name


def test_every_event_name_used_is_a_documented_emission_point() -> None:
    q = json.loads(QUERIES.read_text(encoding="utf-8"))["queries"]
    a = json.loads(ALERTS.read_text(encoding="utf-8"))["monitors"]
    used = _events(q) | _events(a)
    assert used
    missing = {e for e in used if f"`{e}`" not in EMISSION}
    assert not missing, missing


def test_monitors_only_search_the_app_indices() -> None:
    for m in json.loads(ALERTS.read_text(encoding="utf-8"))["monitors"]:
        assert m["inputs"][0]["search"]["indices"] == ["avp-app-prod-*"]
