"""日次異常レポート（読み取り専用）の語彙。カテゴリと集計窓の単一宣言元。

実装は ``domain/anomaly_report.py``（純粋な集計）、``infrastructure/diagnostics/anomaly_events.py``
（SELECT だけの読み取り）、CLI は ``scripts/daily-anomaly-report.py``。
"""

from __future__ import annotations

from enum import StrEnum


class AnomalyCategory(StrEnum):
    NOT_COMPLETED_BY_DEADLINE = "not_completed_by_deadline"
    STAGE_STALLED = "stage_stalled"
    NOT_UPLOADED_BY_DEADLINE = "not_uploaded_by_deadline"
    #: 外部 provider が拒否・認証失敗で返した job の失敗
    PROVIDER_ERROR = "provider_error"
    #: Temporal の workflow が failed で閉じた（日次 Schedule の起動分）
    WORKFLOW_FAILURE = "workflow_failure"
    RENDER_FAILURE = "render_failure"
    UPLOAD_FAILURE = "upload_failure"
    #: 日次 slot（Episode）が作られなかった日
    MISSED_SCHEDULE_RUN = "missed_schedule_run"


#: 集計窓（名前 -> 時間）。``None`` は全期間。表示順もこの順。
REPORT_WINDOWS_HOURS: dict[str, int | None] = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24, "all": None}

#: 「いま再発している」とみなす窓（release preflight が見る値）
RECURRENCE_WINDOW = "24h"
