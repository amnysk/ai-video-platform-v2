"""ログ契約の語彙そのものの整合（ADR-0040 / docs/observability/log-contract.md）。

mapping との一致は別の contract test、発行側の形式は unit test が検査する。
ここは「唯一の宣言元」が自己矛盾していないことだけを見る。
"""

from __future__ import annotations

from pathlib import Path

from contracts.logging import (
    LOG_FIELD_NAMES,
    LOG_FIELDS,
    REQUIRED_APP_FIELDS,
    EventName,
    FieldOrigin,
)

REPO = Path(__file__).resolve().parents[2]


def test_field_names_are_unique() -> None:
    names = [f.name for f in LOG_FIELDS]
    assert len(names) == len(set(names))
    assert set(names) == LOG_FIELD_NAMES


def test_required_fields_are_written_by_the_app() -> None:
    required = {f.name for f in LOG_FIELDS if f.always}
    assert set(REQUIRED_APP_FIELDS) == required
    assert all(f.origin is FieldOrigin.APP for f in LOG_FIELDS if f.always)
    assert {"@timestamp", "schema_version", "event_id", "event_name", "level"} <= required
    assert "message" in required
    assert {"service_name", "environment", "git_sha"} <= required


def test_every_event_name_is_documented() -> None:
    """EventName を足して log-contract.md の一覧を更新し忘れる片側更新を防ぐ（AGENTS.md §7）。"""
    doc = (REPO / "docs" / "observability" / "log-contract.md").read_text(encoding="utf-8")
    missing = [e.value for e in EventName if e.value not in doc]
    assert not missing, f"log-contract.md に載っていない event_name: {missing}"


def test_every_field_is_documented() -> None:
    doc = (REPO / "docs" / "observability" / "log-contract.md").read_text(encoding="utf-8")
    missing = [f.name for f in LOG_FIELDS if f"`{f.name}`" not in doc]
    assert not missing, f"log-contract.md に載っていないフィールド: {missing}"
