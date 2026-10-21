"""日次異常の窓付き・重複除去つき集計（純粋。I/O なし）。

同じ停滞 Episode は watchdog が毎時検出し続けるため、検出の行数を数えると「新しい失敗」が
増えたように見える。ここでは **incident_key（同じ Episode・同じ工程・同じ原因）** を単位に畳み、
次を分けて返す:

- ``rows``: 窓に入った検出行（重複を含む。従来の「累積250」型の数え方）
- ``incidents``: 窓の中で見えた distinct な incident
- ``new_incidents``: 初検出が窓の中にある incident（本当に新しいもの）
- ``unresolved``: いま未解決の incident（全期間）
- ``recurring_unresolved``: 未解決で、最後の検出が直近 24h にあるもの（いま再発中）
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from contracts.anomaly_report import (
    RECURRENCE_WINDOW,
    REPORT_WINDOWS_HOURS,
    AnomalyCategory,
)


@dataclass(frozen=True)
class AnomalyEvent:
    """検出の1行（DB の1行・Temporal の1実行）。"""

    category: AnomalyCategory
    #: 重複除去キー。同じ incident の再検出は同じ値になる（例 ``<kind>:<episode_id>``）
    incident_key: str
    episode_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    cause: str
    resolved: bool
    #: この行が表す検出回数（watchdog の ``occurrences``）。無ければ 1
    detections: int = 1


def _since(now: datetime, hours: int | None) -> datetime | None:
    return None if hours is None else now - timedelta(hours=hours)


def _latest(events: Iterable[AnomalyEvent]) -> dict[str, AnomalyEvent]:
    latest: dict[str, AnomalyEvent] = {}
    for e in events:
        cur = latest.get(e.incident_key)
        if cur is None or e.last_seen_at > cur.last_seen_at:
            latest[e.incident_key] = e
    return latest


def build_report(
    events: Iterable[AnomalyEvent],
    now: datetime,
    *,
    windows: Mapping[str, int | None] = REPORT_WINDOWS_HOURS,
) -> dict[str, Any]:
    """カテゴリ × 窓の集計。JSON にそのまま出せる dict を返す。"""
    all_events = list(events)
    categories: dict[str, Any] = {}
    for category in AnomalyCategory:
        mine = [e for e in all_events if e.category == category]
        latest = _latest(mine)
        first_seen: dict[str, datetime] = {}
        for e in mine:
            cur = first_seen.get(e.incident_key)
            if cur is None or e.first_seen_at < cur:
                first_seen[e.incident_key] = e.first_seen_at
        unresolved_keys = {k for k, e in latest.items() if not e.resolved}
        recurrence_since = _since(now, windows.get(RECURRENCE_WINDOW, 24))
        recurring = {
            k
            for k in unresolved_keys
            if recurrence_since is None or latest[k].last_seen_at >= recurrence_since
        }
        per_window: dict[str, Any] = {}
        for name, hours in windows.items():
            since = _since(now, hours)
            in_window = [e for e in mine if since is None or e.last_seen_at >= since]
            keys = {e.incident_key for e in in_window}
            new_keys = {k for k in keys if since is None or first_seen[k] >= since}
            per_window[name] = {
                "rows": len(in_window),
                "detections": sum(e.detections for e in in_window),
                "incidents": len(keys),
                "new_incidents": len(new_keys),
                "episodes": len({e.episode_id for e in in_window if e.episode_id}),
            }
        causes = Counter(e.cause for e in latest.values())
        categories[category.value] = {
            "windows": per_window,
            "total_rows": len(mine),
            "total_incidents": len(latest),
            "total_episodes": len({e.episode_id for e in mine if e.episode_id}),
            "unresolved": len(unresolved_keys),
            "recurring_unresolved": len(recurring),
            "still_recurring": bool(recurring),
            "causes": dict(causes.most_common()),
            "common_cause": len(causes) == 1 and len(latest) > 1,
            "duplicate_ratio": (round(1 - len(latest) / len(mine), 4) if mine else 0.0),
        }
    return {"generated_at": now.isoformat(), "categories": categories}


def recurring_unresolved_count(report: Mapping[str, Any]) -> dict[str, int]:
    """release preflight 用: 直近 24h に再検出された未解決 incident 数（カテゴリ別）。"""
    return {name: int(body["recurring_unresolved"]) for name, body in report["categories"].items()}


def total_recurring_unresolved(report: Mapping[str, Any]) -> int:
    return sum(recurring_unresolved_count(report).values())


def render_text(report: Mapping[str, Any]) -> str:
    lines = [f"daily anomaly report  generated_at={report['generated_at']}", ""]
    window_names = list(next(iter(report["categories"].values()))["windows"])
    head = "category".ljust(28) + "".join(f"{w:>14}" for w in window_names)
    lines += [head, "  (cell = incidents/new/rows)", ""]
    for name, body in report["categories"].items():
        cells = ""
        for w in window_names:
            c = body["windows"][w]
            cells += f"{c['incidents']}/{c['new_incidents']}/{c['rows']}".rjust(14)
        lines.append(name.ljust(28) + cells)
    lines.append("")
    for name, body in report["categories"].items():
        if not body["total_rows"]:
            continue
        causes = "; ".join(f"{k} x{v}" for k, v in body["causes"].items())
        lines.append(
            f"{name}: incidents={body['total_incidents']} (rows={body['total_rows']}, "
            f"episodes={body['total_episodes']}) unresolved={body['unresolved']} "
            f"recurring_unresolved_24h={body['recurring_unresolved']}"
        )
        lines.append(f"    causes: {causes}")
    return "\n".join(lines)
