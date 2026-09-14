"""状態値とその語彙の**唯一の定義**（AGENTS.md §8）。

ここ以外のモジュールで同じ集合を literal に書き直さないこと。
派生集合は必ずここで導出する。
"""

from __future__ import annotations

from enum import StrEnum


class EpisodeStatus(StrEnum):
    """docs/domain/episode.md の状態表。"""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    NEEDS_WORK = "needs_work"
    BLOCKED = "blocked"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    UPLOADED = "uploaded"
    ANALYZED = "analyzed"
    COMPLETED = "completed"
    SCRIPT_READY = "script_ready"
    STORYBOARD_READY = "storyboard_ready"
    #: 画像・音声・動画が揃った駐機点（ADR-0017）。Phase 5 Render の入口。
    ASSETS_READY = "assets_ready"
    #: 完成動画が技術検査を通って保存された駐機点（ADR-0019）。Phase 6 Upload の入口。
    RENDER_READY = "render_ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


EPISODE_TERMINAL_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {
        EpisodeStatus.ANALYZED,
        EpisodeStatus.COMPLETED,
        EpisodeStatus.FAILED,
        EpisodeStatus.CANCELLED,
    }
)

EPISODE_ACTIVE_STATUSES: frozenset[EpisodeStatus] = frozenset(EpisodeStatus) - (
    EPISODE_TERMINAL_STATUSES
)


class JobStatus(StrEnum):
    """docs/domain/job.md の状態表。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    SKIPPED = "skipped"


JOB_TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.TERMINAL_FAILED, JobStatus.SKIPPED}
)


class FailureClass(StrEnum):
    """docs/failure-policy.md §1。例外型から導出する。散文のgrepで決めない。"""

    TRANSIENT = "transient"
    RETRYABLE = "retryable"
    NEEDS_INPUT = "needs_input"
    PERMANENT = "permanent"


#: retryを許してよい失敗クラス。permanent / needs_input はretryしない。
RETRYABLE_FAILURE_CLASSES: frozenset[FailureClass] = frozenset(
    {FailureClass.TRANSIENT, FailureClass.RETRYABLE}
)


class JobType(StrEnum):
    """工程の種類。

    job（工程）と artifact（成果物）は同名にしない。「script が失敗した」が
    どちらの話か決まらなくなるため（Phase 2 のレビュー指摘）。
    """

    DUMMY = "dummy"
    WRITE_SCRIPT = "write_script"
    PLAN_STORYBOARD = "plan_storyboard"  # ADR-0015
    # ADR-0017: Production（シーン単位の job は jobs.scene_id で区別する / ADR-0018）
    PRODUCE_SCENE_IMAGE = "produce_scene_image"
    PRODUCE_SCENE_VOICE = "produce_scene_voice"
    PRODUCE_SCENE_VIDEO = "produce_scene_video"
    ASSEMBLE_PRODUCTION = "assemble_production"
    #: ADR-0019: Render（Episode 単位。scene_id は NULL）
    RENDER_FINAL_VIDEO = "render_final_video"


class ArtifactType(StrEnum):
    """Artifactの種類。docs/domain/artifact.md の表に対応する。"""

    DUMMY = "dummy"
    SCRIPT = "script"
    STORYBOARD = "storyboard"  # ADR-0015
    # ADR-0017 / ADR-0018: シーン単位の成果物（artifact_metadata.scene_id で区別する）
    SCENE_IMAGE = "scene_image"
    SCENE_VOICE = "scene_voice"
    SCENE_VIDEO = "scene_video"
    PRODUCTION_MANIFEST = "production_manifest"
    #: ADR-0019: 完成動画（Episode 単位。scene_id は NULL）
    FINAL_VIDEO = "final_video"


class Pipeline(StrEnum):
    """どの workflow を起動するか。

    workflow 名と task queue の対応は ``PIPELINE_WORKFLOWS`` が唯一の定義元。
    """

    SKELETON = "skeleton"
    SCRIPT = "script"


#: パイプライン -> (workflow名, task queue)。API と worker がここから導出する。
PIPELINE_WORKFLOWS: dict[Pipeline, tuple[str, str]] = {
    Pipeline.SKELETON: ("EpisodeSkeletonWorkflow", "episode-skeleton"),
    Pipeline.SCRIPT: ("ScriptWorkflow", "script"),
}

#: storyboard 工程の (workflow名, task queue)（ADR-0015）。
#: ``Pipeline`` には載せない。Pipeline は Episode **作成時**に起動する workflow の語彙で、
#: storyboard は既存 Episode（``script_ready``）に対して起動するため。
STORYBOARD_WORKFLOW: tuple[str, str] = ("StoryboardWorkflow", "storyboard")

#: production 工程の (workflow名, task queue)（ADR-0017）。
#: storyboard と同じ理由で Pipeline に載せない。
PRODUCTION_WORKFLOW: tuple[str, str] = ("ProductionWorkflow", "production")

#: production 工程へ入ってよい Episode 状態（ADR-0017）。駐機点に加え、production 自身の
#: 失敗で止まった ``needs_work`` / ``blocked`` からの再実行（人間の POST が再開の操作）。
#: 判定の権威は workflow の admit Activity。API はこれで早めに 409 を返すだけ。
PRODUCTION_ADMISSIBLE_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {
        EpisodeStatus.STORYBOARD_READY,
        EpisodeStatus.ASSETS_READY,
        EpisodeStatus.NEEDS_WORK,
        EpisodeStatus.BLOCKED,
    }
)

#: メディア種別ごとの task queue（ADR-0017）。並行数は worker 側の設定で queue ごとに決める。
PRODUCTION_IMAGE_TASK_QUEUE = "production-image"
PRODUCTION_VOICE_TASK_QUEUE = "production-voice"
PRODUCTION_VIDEO_TASK_QUEUE = "production-video"

#: render 工程の (workflow名, task queue)（ADR-0019）。storyboard と同じ理由で Pipeline に載せない。
RENDER_WORKFLOW: tuple[str, str] = ("RenderWorkflow", "render")
#: render の task queue。workflow・状態系 Activity・重い描画 Activity が共有する（ADR-0019 §10）。
RENDER_TASK_QUEUE: str = RENDER_WORKFLOW[1]

#: render 工程へ入ってよい Episode 状態（ADR-0019）。駐機点 ``assets_ready``、再描画の
#: ``render_ready``、render 自身の失敗で止まった ``needs_work`` / ``blocked``
#: （production と同じ規則）。
#: 判定の権威は workflow の admit Activity。API はこれで早めに 409 を返すだけ。
RENDER_ADMISSIBLE_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {
        EpisodeStatus.ASSETS_READY,
        EpisodeStatus.RENDER_READY,
        EpisodeStatus.NEEDS_WORK,
        EpisodeStatus.BLOCKED,
    }
)


class ProviderCall(StrEnum):
    """予約台帳が扱う外部呼び出しの種類（ADR-0013）。

    provider を増やすときはここに足す。台帳のテーブル定義は共有する。
    """

    CODEX_SCRIPT = "codex_script"
    #: 台本と値を分け、未照合予約の検査を工程ごとに独立させる（ADR-0015）。
    CODEX_STORYBOARD = "codex_storyboard"
    #: 有料の非同期ジョブ型 provider（ADR-0017）。ローカルの非課金 TTS は台帳に載せない。
    FAL_IMAGE = "fal_image"
    FAL_VIDEO = "fal_video"


class ReservationStatus(StrEnum):
    """予約台帳の状態（ADR-0013）。

    ``reserved`` から自動で出られるのは evidence による照合だけ。
    「自動で解放しない」は遷移表に辺を**書かないこと**で表現する。
    """

    RESERVED = "reserved"
    SPENT = "spent"
    ABANDONED = "abandoned"


RESERVATION_TERMINAL_STATUSES: frozenset[ReservationStatus] = frozenset(
    {ReservationStatus.SPENT, ReservationStatus.ABANDONED}
)


DEFAULT_MAX_ATTEMPTS = 3
