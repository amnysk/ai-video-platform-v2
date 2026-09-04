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


class ArtifactType(StrEnum):
    """Artifactの種類。docs/domain/artifact.md の表に対応する。"""

    DUMMY = "dummy"
    SCRIPT = "script"


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


class ProviderCall(StrEnum):
    """予約台帳が扱う外部呼び出しの種類（ADR-0013）。

    provider を増やすときはここに足す。台帳のテーブル定義は共有する。
    """

    CODEX_SCRIPT = "codex_script"


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
