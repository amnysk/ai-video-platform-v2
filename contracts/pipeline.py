"""Daily Schedule と Episode pipeline の契約（ADR-0023）。

workflow（``workers/pipeline``）・Schedule 登録（``infrastructure/temporal/schedules``）・CLI が
共有するのは**この名前と型だけ**である（INV-3）。

- 工程の順序は ``EpisodePipelineWorkflow`` だけが持つ（INV-4 / INV-5）。ここは語彙を並べるだけ
- 工程の workflow id は API の起動（``apps/api/workflow_starter.py``）と同じ規約。同じ Episode の
  同じ工程を API と pipeline が二重に走らせない（Temporal が同じ id の実行を拒否する）
- 出力 profile は運ぶ（``render_profile_id``）。Shorts を前提にしない
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from contracts.operations import ClaimOutcome
from contracts.production_activities import (
    DEFAULT_AWAIT_REEXECUTIONS,
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_MAX_ROUNDS,
    DEFAULT_VIDEO_CONCURRENCY,
    DEFAULT_VIDEO_MAX_ROUNDS,
    DEFAULT_VOICE_CONCURRENCY,
)
from contracts.render import DEFAULT_RENDER_PROFILE_ID
from contracts.states import (
    PIPELINE_WORKFLOWS,
    PRODUCTION_WORKFLOW,
    RENDER_WORKFLOW,
    STORYBOARD_WORKFLOW,
    UPLOAD_WORKFLOW,
    EpisodeStatus,
    Pipeline,
)
from contracts.topic_planning import (
    DEFAULT_CONTENT_PROFILE_ID,
    DEFAULT_STRATEGY_PROFILE_ID,
    TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS,
    TOPIC_PLANNER_WORKFLOW,
)

# ------------------------------------------------------------------ workflow 名・queue・Schedule

PIPELINE_TASK_QUEUE = "pipeline"
DAILY_EPISODE_WORKFLOW: tuple[str, str] = ("DailyEpisodeWorkflow", PIPELINE_TASK_QUEUE)
EPISODE_PIPELINE_WORKFLOW: tuple[str, str] = ("EpisodePipelineWorkflow", PIPELINE_TASK_QUEUE)

#: 本番の Schedule id。テストはこの id を**使わない**（一意な id を作る）。
DAILY_SCHEDULE_ID = "avp-daily-episode"
#: Schedule が起動する DailyEpisodeWorkflow の id の接頭辞（Temporal が時刻を後ろに付ける）。
DAILY_WORKFLOW_ID_PREFIX = "daily-episode"

# ----------------------------------------------------------------- 既定値の唯一の宣言元

DEFAULT_DAILY_EPISODE_LIMIT = 1
#: 毎日 06:00（``DEFAULT_SCHEDULE_TIMEZONE``）
DEFAULT_DAILY_SCHEDULE_CRON = "0 6 * * *"
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Tokyo"
#: サーバ停止中に取りこぼした起動を後から実行してよい幅（秒）。これより古い分は捨てる。
DEFAULT_SCHEDULE_CATCHUP_WINDOW_SECONDS = 60 * 60
#: 同じ id の Topic Planner がすでに走っていたとき（別の Daily 実行が起動中）の待ち方。
#: 待つ間隔（秒）と、起動を試みる最大回数。使い切ったら Daily は失敗する（Episode を作らない）。
#: 回数は Planner の execution timeout から導く
#: （走行中の Planner が timeout で終わるまで待ち切れる）
TOPIC_PLANNER_BUSY_WAIT_SECONDS = 60
TOPIC_PLANNER_START_ATTEMPTS = (
    -(-TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS // TOPIC_PLANNER_BUSY_WAIT_SECONDS) + 1
)

# --------------------------------------------------------------------------- Activity 名

PIPELINE_CHECK_PAUSED = "pipeline_check_paused"
PIPELINE_CLAIM_DAILY_SLOT = "pipeline_claim_daily_slot"
PIPELINE_UPLOAD_GATE = "pipeline_upload_gate"

#: claim の結果の語彙は ``contracts.operations.ClaimOutcome``（ADR-0021）が唯一の宣言元（再輸出）
RE_EXPORTED = (ClaimOutcome,)

PIPELINE_ACTIVITY_NAMES: tuple[str, ...] = (
    PIPELINE_CHECK_PAUSED,
    PIPELINE_CLAIM_DAILY_SLOT,
    PIPELINE_UPLOAD_GATE,
)


class PipelineStage(StrEnum):
    """pipeline が順に起動する工程。**順序はこの定義順**（workflow がこれを辿る）。"""

    SCRIPT = "script"
    STORYBOARD = "storyboard"
    PRODUCTION = "production"
    RENDER = "render"
    UPLOAD = "upload"


#: 工程が成功したときに Episode が止まる駐機点。これ以外の状態が返ったら pipeline は止まる。
STAGE_PARKING_STATUS: dict[PipelineStage, EpisodeStatus] = {
    PipelineStage.SCRIPT: EpisodeStatus.SCRIPT_READY,
    PipelineStage.STORYBOARD: EpisodeStatus.STORYBOARD_READY,
    PipelineStage.PRODUCTION: EpisodeStatus.ASSETS_READY,
    PipelineStage.RENDER: EpisodeStatus.RENDER_READY,
    PipelineStage.UPLOAD: EpisodeStatus.UPLOADED,
}


class DailyOutcome(StrEnum):
    STARTED = "started"
    #: 同じ pipeline id がすでに走っている / 完了している（二重起動しない）
    ALREADY_STARTED = "already_started"
    PAUSED = "paused"
    LIMIT_REACHED = "limit_reached"
    #: claim が返した Episode に TopicPlan が結び付いていない。pipeline を始めない（INV-21）
    NO_TOPIC_PLAN = "no_topic_plan"


class PipelineOutcome(StrEnum):
    COMPLETED = "completed"
    #: 工程が駐機点以外を返した / 失敗した / 同じ工程がすでに走っていた
    STOPPED = "stopped"
    #: render_ready まで来たが投稿ゲートが拒否した
    UPLOAD_SKIPPED = "upload_skipped"


# --------------------------------------------------------------------------- id 規約


def script_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}"


def storyboard_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-storyboard"


def production_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-production"


def render_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-render"


def upload_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-upload"


def pipeline_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-pipeline"


def local_slot_date(instant: datetime, timezone: str) -> date:
    """起動時刻を運用タイムゾーンの日付にする（「1日1本」の日付の定義）。"""
    return instant.astimezone(ZoneInfo(timezone)).date()


# --------------------------------------------------------------------------- 入出力


@dataclass
class ProductionParameters:
    """ProductionWorkflow に渡す並行枠と予算（``ProductionWorkflowInput`` と同じ名前）。"""

    image_concurrency: int = DEFAULT_IMAGE_CONCURRENCY
    video_concurrency: int = DEFAULT_VIDEO_CONCURRENCY
    voice_concurrency: int = DEFAULT_VOICE_CONCURRENCY
    image_max_rounds: int = DEFAULT_IMAGE_MAX_ROUNDS
    video_max_rounds: int = DEFAULT_VIDEO_MAX_ROUNDS
    await_reexecutions: int = DEFAULT_AWAIT_REEXECUTIONS


def _script_workflow() -> tuple[str, str]:
    return PIPELINE_WORKFLOWS[Pipeline.SCRIPT]


@dataclass
class PipelineOptions:
    """Episode を問わない pipeline の設定。Schedule の入力に載せて毎日同じものを使う。"""

    render_profile_id: str = DEFAULT_RENDER_PROFILE_ID
    production: ProductionParameters = field(default_factory=ProductionParameters)
    #: 工程の (workflow 名, task queue)。テストが fake の workflow に差し替える
    script_workflow: tuple[str, str] = field(default_factory=_script_workflow)
    storyboard_workflow: tuple[str, str] = STORYBOARD_WORKFLOW
    production_workflow: tuple[str, str] = PRODUCTION_WORKFLOW
    render_workflow: tuple[str, str] = RENDER_WORKFLOW
    upload_workflow: tuple[str, str] = UPLOAD_WORKFLOW
    #: Episode 作成の前に走る Topic Planner（ADR-0025）
    topic_planner_workflow: tuple[str, str] = TOPIC_PLANNER_WORKFLOW
    #: EpisodePipelineWorkflow 自身の task queue
    pipeline_task_queue: str = PIPELINE_TASK_QUEUE


@dataclass
class DailyEpisodeInput:
    daily_limit: int = DEFAULT_DAILY_EPISODE_LIMIT
    timezone: str = DEFAULT_SCHEDULE_TIMEZONE
    #: ISO 日付（``YYYY-MM-DD``）。手動・テスト用。空なら起動時刻から導出する
    slot_date: str | None = None
    #: 旧入力（ADR-0023）。Topic は Topic Planner が決める（ADR-0025）ので**使わない**。
    #: 旧 Schedule の入力を decode できるよう残す
    topic: str | None = None
    options: PipelineOptions = field(default_factory=PipelineOptions)
    strategy_profile_id: str = DEFAULT_STRATEGY_PROFILE_ID
    content_profile_id: str = DEFAULT_CONTENT_PROFILE_ID


@dataclass
class DailyEpisodeResult:
    outcome: str
    slot_date: str
    episode_id: str | None = None
    pipeline_workflow_id: str | None = None
    topic_plan_id: str | None = None
    reason: str | None = None


@dataclass
class EpisodePipelineInput:
    episode_id: str
    options: PipelineOptions = field(default_factory=PipelineOptions)


@dataclass
class EpisodePipelineResult:
    episode_id: str
    outcome: str
    #: 最後に分かった Episode 状態（**application domain state** / INV-8）。不明なら空文字
    status: str
    completed_stages: list[str] = field(default_factory=list)
    stopped_stage: str | None = None
    reason: str | None = None


@dataclass
class CheckPausedRequest:
    #: True なら ``uploads_paused`` も見る（投稿ゲート用）
    include_uploads: bool = False


@dataclass
class CheckPausedResult:
    paused: bool
    reason: str | None = None


@dataclass
class ClaimDailySlotRequest:
    slot_date: str
    trigger_id: str
    daily_limit: int
    topic: str | None = None
    #: 新しい Episode に結び付ける TopicPlan（ADR-0025）。RESUME で Plan の無い planned Episode を
    #: 再開するときも、これを結び付けてから返す
    topic_plan_id: str | None = None


@dataclass
class ClaimDailySlotResult:
    outcome: str
    episode_id: str | None = None
    #: 返した Episode に結び付いている TopicPlan。None の Episode の pipeline は始めない（INV-21）
    topic_plan_id: str | None = None


@dataclass
class UploadGateRequest:
    episode_id: str


@dataclass
class UploadGateResult:
    allowed: bool
    reason: str | None = None
    #: 判定時点の Episode 状態。Episode が無ければ空文字
    status: str = ""
