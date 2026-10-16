"""記録を作った Activity の Temporal 識別子（判断記録・実行記録の結び付き用）。

``agent_runs`` / ``decision_records`` の ``workflow_id`` / ``workflow_run_id`` / ``activity_id``
（migration 0018）の取得元。**Activity の内側でだけ**値を返し、外側（test・CLI・API・
Topic の staging）は ``None``（補完しない）。Activity の入力の形は変えない（replay 互換）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity


@dataclass(frozen=True, slots=True)
class ActivityIds:
    workflow_id: str | None
    workflow_run_id: str | None
    activity_id: str | None

    def event_refs(self) -> dict[str, Any]:
        """``episode_events.refs`` に足す項目（ログ既存の workflow_id / run_id / activity_id）"""
        refs = {
            "workflow_id": self.workflow_id,
            "run_id": self.workflow_run_id,
            "activity_id": self.activity_id,
        }
        return {k: v for k, v in refs.items() if v is not None}


def current_activity_ids() -> ActivityIds | None:
    """今 Activity の中なら識別子。外なら ``None``。例外を外へ出さない（記録を壊さない）。"""
    try:
        if not activity.in_activity():
            return None
        info = activity.info()
    except Exception:
        return None
    return ActivityIds(
        workflow_id=info.workflow_id or None,
        workflow_run_id=info.workflow_run_id or None,
        activity_id=info.activity_id or None,
    )


__all__ = ["ActivityIds", "current_activity_ids"]
