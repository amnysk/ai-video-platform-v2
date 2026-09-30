"""ログ契約の語彙そのものの整合（ADR-0040 / docs/observability/log-contract.md）。

mapping との一致は別の contract test、発行側の形式は unit test が検査する。
ここは「唯一の宣言元」が自己矛盾していないことだけを見る。
"""

from __future__ import annotations

from pathlib import Path

from contracts.log_contract import (
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
    missing = [e.value for e in EventName if f"`{e.value}`" not in doc]
    assert not missing, f"log-contract.md に載っていない event_name: {missing}"


def test_every_field_is_documented() -> None:
    doc = (REPO / "docs" / "observability" / "log-contract.md").read_text(encoding="utf-8")
    missing = [f.name for f in LOG_FIELDS if f"`{f.name}`" not in doc]
    assert not missing, f"log-contract.md に載っていないフィールド: {missing}"


def test_rejection_categories_are_spelled_the_same() -> None:
    """ErrorCategory は RejectionCategory の値を写している。食い違いを止める（AGENTS.md §8）。"""
    from contracts.log_contract import ErrorCategory
    from contracts.states import RejectionCategory

    assert {c.value for c in RejectionCategory} <= {c.value for c in ErrorCategory}


def test_provider_labels_do_not_collide_with_ledger_providers() -> None:
    from contracts.log_contract import ProviderLabel
    from contracts.states import ProviderCall

    assert not {p.value for p in ProviderLabel} & {p.value for p in ProviderCall}
    doc = (REPO / "docs" / "observability" / "log-contract.md").read_text(encoding="utf-8")
    missing = [p.value for p in ProviderLabel if f"`{p.value}`" not in doc]
    assert not missing, f"log-contract.md に載っていない provider ラベル: {missing}"
