"""日次異常レポートの集計（窓・重複除去・再発判定）。固定の行だけで検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from contracts.anomaly_report import AnomalyCategory
from domain.anomaly_report import (
    AnomalyEvent,
    build_report,
    recurring_unresolved_count,
    render_text,
    total_recurring_unresolved,
)

NOW = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
STALL = AnomalyCategory.STAGE_STALLED


def _ev(key, days_ago_first, days_ago_last, *, resolved=False, cause="c", n=1, cat=STALL, ep=None):
    return AnomalyEvent(
        cat,
        key,
        ep or key,
        NOW - timedelta(days=days_ago_first),
        NOW - timedelta(days=days_ago_last),
        cause,
        resolved,
        n,
    )


def _cat(report, cat=STALL):
    return report["categories"][cat.value]


def test_daily_redetection_of_one_stuck_episode_is_one_incident_not_many_rows() -> None:
    # 同じ Episode を毎日検出した10行 = incident は1つ（「累積」を失敗数と読まない）
    events = [_ev("ep1", 9 - i, 9 - i, n=24) for i in range(10)]
    body = _cat(build_report(events, NOW))
    assert body["total_rows"] == 10
    assert body["total_incidents"] == 1
    assert body["duplicate_ratio"] == 0.9
    assert body["windows"]["all"]["incidents"] == 1
    assert body["windows"]["all"]["detections"] == 240


def test_windows_count_rows_in_window_and_new_incidents_only_when_first_seen_inside() -> None:
    old = [_ev("old", 20, d) for d in (20, 5, 2, 0.5)]  # 20日前に初検出、いまも再検出
    new = [_ev("new", 0.5, 0.5)]
    w = _cat(build_report([*old, *new], NOW))["windows"]
    assert (w["24h"]["rows"], w["24h"]["incidents"], w["24h"]["new_incidents"]) == (2, 2, 1)
    assert (w["7d"]["rows"], w["7d"]["incidents"], w["7d"]["new_incidents"]) == (4, 2, 1)
    assert (w["30d"]["incidents"], w["30d"]["new_incidents"]) == (2, 2)
    assert w["all"]["rows"] == 5


def test_recurring_unresolved_needs_unresolved_and_a_detection_within_24h() -> None:
    events = [
        _ev("stuck_now", 5, 0.1),  # 未解決・いま再検出
        _ev("stuck_old", 30, 3),  # 未解決だが3日前で止まった（自然に消えた/検出されなくなった）
        _ev("fixed", 5, 0.1, resolved=True),  # 解決済み
    ]
    body = _cat(build_report(events, NOW))
    assert body["unresolved"] == 2
    assert body["recurring_unresolved"] == 1
    assert body["still_recurring"] is True


def test_resolution_is_judged_by_the_latest_row_of_an_incident() -> None:
    events = [_ev("ep", 5, 5), _ev("ep", 5, 0.1, resolved=True)]
    body = _cat(build_report(events, NOW))
    assert body["unresolved"] == 0
    assert body["recurring_unresolved"] == 0


def test_common_cause_is_flagged_only_when_many_incidents_share_one_cause() -> None:
    same = [_ev(f"e{i}", 1, 1, cause="403 credentials") for i in range(3)]
    assert _cat(build_report(same, NOW))["common_cause"] is True
    mixed = [*same, _ev("x", 1, 1, cause="other")]
    body = _cat(build_report(mixed, NOW))
    assert body["common_cause"] is False
    assert body["causes"] == {"403 credentials": 3, "other": 1}


def test_every_category_is_present_even_without_events() -> None:
    report = build_report([], NOW)
    assert set(report["categories"]) == {c.value for c in AnomalyCategory}
    assert total_recurring_unresolved(report) == 0
    assert "daily anomaly report" in render_text(report)


def test_preflight_entry_returns_counts_by_category() -> None:
    events = [
        _ev("a", 2, 0.1, cat=AnomalyCategory.RENDER_FAILURE),
        _ev("b", 2, 0.1, cat=AnomalyCategory.RENDER_FAILURE),
        _ev("c", 2, 0.1, cat=AnomalyCategory.UPLOAD_FAILURE, resolved=True),
    ]
    counts = recurring_unresolved_count(build_report(events, NOW))
    assert counts["render_failure"] == 2
    assert counts["upload_failure"] == 0
    assert set(counts) == {c.value for c in AnomalyCategory}
