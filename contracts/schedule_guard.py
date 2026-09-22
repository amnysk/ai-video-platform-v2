"""Daily Schedule の「本番の望ましい状態」と、その監視の語彙（ADR-0027）。

ここが唯一の宣言元（AGENTS.md §8）。DB の CHECK・CLI・watchdog はここから導出する。

- 本番では ``avp-daily-episode`` は **paused=false** が原則（``DESIRED_DAILY_SCHEDULE_PAUSED``）
- 例外は2種類だけで、Schedule の pause note で区別する
  - **maintenance pause**: ガードが作った一時停止。note が ``MAINTENANCE_NOTE_PREFIX`` で始まり、
    期限（deadline）を持つ。ガードだけが解除してよい
  - **emergency pause**: それ以外のすべての pause（運用者の手動停止）。**自動では絶対に解除しない**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: 本番の望ましい状態。Schedule は動いている（paused=False）
DESIRED_DAILY_SCHEDULE_PAUSED = False

#: watchdog 自身の Schedule（別 id。daily の pause の影響を受けない）
WATCHDOG_SCHEDULE_ID = "avp-daily-watchdog"
WATCHDOG_WORKFLOW: tuple[str, str] = ("DailyWatchdogWorkflow", "pipeline")
WATCHDOG_WORKFLOW_ID_PREFIX = "daily-watchdog"
#: 毎時 35 分。06:00 の daily に対し、最初の検査が猶予 30 分後（06:35）に当たる
DEFAULT_WATCHDOG_CRON = "35 * * * *"
#: 予定時刻からこの時間が過ぎても daily が始まっていなければ異常
DEFAULT_WATCHDOG_GRACE_SECONDS = 30 * 60

#: maintenance pause の note 接頭辞。note は ``<接頭辞><JSON>``
MAINTENANCE_NOTE_PREFIX = "AVP-MAINTENANCE/1 "
DEFAULT_MAINTENANCE_TTL_SECONDS = 30 * 60
#: 忘れても最大この時間で watchdog が解除できる（それ以上の TTL は拒否）
MAX_MAINTENANCE_TTL_SECONDS = 6 * 60 * 60
#: 解除時に Schedule へ書く note
RELEASED_NOTE = "avp: released by schedule guard"

#: daily（1日1回）の Schedule で、次回実行が現在からこれ以内にあること
DAILY_NEXT_RUN_MAX_GAP_SECONDS = 25 * 60 * 60

WATCHDOG_CHECK_ACTIVITY = "pipeline_watchdog_check"

# --------------------------------------------------------- Episode 進行監視（ADR-0031、単一宣言元）

#: blocked / needs_work のまま、この分数を超えたら停滞とみなす。工程非依存の単一値から開始する
#: （将来 Shorts 以外の尺・工程が増えたら工程別に分ける）。
DEFAULT_STAGE_STALL_GRACE_MINUTES = 60
#: Episode 作成からこの時間を超えても render_ready 以降に達していなければ完成期限超過。
DEFAULT_COMPLETION_DEADLINE_HOURS = 8.0
#: render_ready / approved 到達からこの時間を超えても uploaded に達していなければ投稿期限超過
#: （ただし UPLOADS_PAUSED が有効な間は意図した停止として anomaly にしない）。
DEFAULT_UPLOAD_DEADLINE_HOURS = 2.0


class ScheduleHealth(StrEnum):
    """``avp-daily-episode`` の分類。"""

    #: 動いていて、次回実行が妥当
    HEALTHY = "healthy"
    #: ガードの maintenance pause の期限内（作業中）
    MAINTENANCE_IN_PROGRESS = "maintenance_in_progress"
    #: ガードの maintenance pause の期限切れ（解除してよい）
    MAINTENANCE_EXPIRED = "maintenance_expired"
    #: 印の無い pause（emergency / 手動）。自動では触らない
    PAUSED_UNEXPECTEDLY = "paused_unexpectedly"
    #: 動いているが次回実行が無い・遠すぎる
    NEXT_RUN_INVALID = "next_run_invalid"
    #: Schedule が存在しない
    MISSING = "missing"


class AnomalyKind(StrEnum):
    """DB（``operational_anomalies``）に残す運用異常。値は grep できる固定キー。"""

    #: 予定時刻を過ぎても、その日の daily slot も DailyEpisodeWorkflow も無い
    DAILY_AUTOMATION_NOT_STARTED = "DAILY_AUTOMATION_NOT_STARTED"
    #: 印の無い pause（emergency / 手動）が続いている
    SCHEDULE_PAUSED_UNEXPECTEDLY = "SCHEDULE_PAUSED_UNEXPECTEDLY"
    #: maintenance pause が期限を過ぎて残っていた（ガードが解除した）
    SCHEDULE_MAINTENANCE_OVERRUN = "SCHEDULE_MAINTENANCE_OVERRUN"
    #: Schedule が存在しない
    SCHEDULE_MISSING = "SCHEDULE_MISSING"
    #: 動いているが次回実行が妥当でない
    SCHEDULE_NEXT_RUN_INVALID = "SCHEDULE_NEXT_RUN_INVALID"
    #: Episode が blocked / needs_work のまま停滞猶予を超えた（ADR-0031、episode_id 付き）
    EPISODE_STAGE_STALLED = "EPISODE_STAGE_STALLED"
    #: Episode が完成期限を超えても render_ready 以降に達していない（ADR-0031、episode_id 付き）
    EPISODE_NOT_COMPLETED_BY_DEADLINE = "EPISODE_NOT_COMPLETED_BY_DEADLINE"
    #: Episode が投稿期限を超えても uploaded に達していない（ADR-0031、episode_id 付き）
    EPISODE_NOT_UPLOADED_BY_DEADLINE = "EPISODE_NOT_UPLOADED_BY_DEADLINE"
    #: pipeline workflow が Temporal 上は completed なのに outcome=stopped で、
    #: DB 側の他のどの検査にも引っかからない食い違い（ADR-0031、episode_id 付き）
    PIPELINE_OUTCOME_MISMATCH = "PIPELINE_OUTCOME_MISMATCH"


#: ``ScheduleHealth`` → 記録する異常（無ければ異常ではない）
HEALTH_ANOMALY: dict[ScheduleHealth, AnomalyKind | None] = {
    ScheduleHealth.HEALTHY: None,
    ScheduleHealth.MAINTENANCE_IN_PROGRESS: None,
    ScheduleHealth.MAINTENANCE_EXPIRED: AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN,
    ScheduleHealth.PAUSED_UNEXPECTEDLY: AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY,
    ScheduleHealth.NEXT_RUN_INVALID: AnomalyKind.SCHEDULE_NEXT_RUN_INVALID,
    ScheduleHealth.MISSING: AnomalyKind.SCHEDULE_MISSING,
}


class DailyStartStatus(StrEnum):
    """その日の daily automation が始まったか。"""

    #: まだ予定時刻 + 猶予に達していない
    NOT_DUE = "not_due"
    #: daily_episode_slot がある
    STARTED_SLOT = "started_slot"
    #: DailyEpisodeWorkflow の実行がある（slot は無いが起動はした）
    STARTED_WORKFLOW = "started_workflow"
    #: どちらも無い
    NOT_STARTED = "not_started"
    #: cron が単純な日次でないので判定しない
    UNSUPPORTED_CRON = "unsupported_cron"


@dataclass(frozen=True, slots=True)
class MaintenanceMarker:
    """Schedule の pause note に載せる maintenance の印。"""

    reason: str
    #: ISO 8601 UTC（``2026-09-20T07:00:00Z``）
    deadline: str
    owner: str = "schedule-guard"


@dataclass
class WatchdogRequest:
    """``DailyWatchdogWorkflow`` の入力。"""

    schedule_id: str = "avp-daily-episode"
    cron: str = "0 6 * * *"
    timezone: str = "Asia/Tokyo"
    grace_seconds: int = DEFAULT_WATCHDOG_GRACE_SECONDS
    workflow_type: str = "DailyEpisodeWorkflow"
    pipeline_workflow_type: str = "EpisodePipelineWorkflow"
    stage_stall_grace_minutes: int = DEFAULT_STAGE_STALL_GRACE_MINUTES
    completion_deadline_hours: float = DEFAULT_COMPLETION_DEADLINE_HOURS
    upload_deadline_hours: float = DEFAULT_UPLOAD_DEADLINE_HOURS


@dataclass
class WatchdogCheckRequest:
    """``pipeline_watchdog_check`` Activity の入力。``now`` は workflow が渡す（決定論）。"""

    now: str
    schedule_id: str = "avp-daily-episode"
    cron: str = "0 6 * * *"
    timezone: str = "Asia/Tokyo"
    grace_seconds: int = DEFAULT_WATCHDOG_GRACE_SECONDS
    workflow_type: str = "DailyEpisodeWorkflow"
    pipeline_workflow_type: str = "EpisodePipelineWorkflow"
    stage_stall_grace_minutes: int = DEFAULT_STAGE_STALL_GRACE_MINUTES
    completion_deadline_hours: float = DEFAULT_COMPLETION_DEADLINE_HOURS
    upload_deadline_hours: float = DEFAULT_UPLOAD_DEADLINE_HOURS


@dataclass
class WatchdogResult:
    schedule_health: str
    daily_start: str
    #: 今回新しく記録した異常（``AnomalyKind`` の値）
    anomalies: list[str] = field(default_factory=list)
    #: 今回、ガードが maintenance pause を解除した
    released_maintenance: bool = False
    slot_date: str = ""
    #: 今回検査した Episode 進行・完成・投稿の異常件数（起動系と別集計。監視の可視化用）
    episode_anomaly_count: int = 0
