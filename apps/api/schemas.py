"""APIの入出力スキーマ。domain state だけを外へ出す（INV-8）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType, Pipeline
from contracts.topic import TOPIC_MAX_CHARS


class CreateEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = Field(default=None, max_length=TOPIC_MAX_CHARS)
    #: 既定は Phase 1 の骨組み。Phase 1 の smoke を壊さないため。
    pipeline: Pipeline = Pipeline.SKELETON


class CreateEpisodeResponse(BaseModel):
    id: str
    status: EpisodeStatus
    workflow_id: str


class StartStoryboardResponse(BaseModel):
    episode_id: str
    #: 起動を受け付けた時点の状態。工程に入れたかは workflow が決める。
    status: EpisodeStatus
    workflow_id: str


class StartProductionResponse(BaseModel):
    episode_id: str
    #: 起動を受け付けた時点の状態。工程に入れたかは workflow の admit が決める。
    status: EpisodeStatus
    workflow_id: str


class StartRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 省略時は ``DEFAULT_RENDER_PROFILE_ID``（ADR-0019）
    render_profile_id: str | None = Field(default=None, max_length=64)


class StartRenderResponse(BaseModel):
    episode_id: str
    #: 起動を受け付けた時点の状態。工程に入れたかは workflow の admit が決める。
    status: EpisodeStatus
    workflow_id: str
    render_profile_id: str


class StartUploadResponse(BaseModel):
    episode_id: str
    #: 起動を受け付けた時点の状態。工程に入れたかは workflow の admit が決める。
    status: EpisodeStatus
    workflow_id: str


class ResumePlanResponse(BaseModel):
    """``GET /episodes/{id}/resume/plan``（ADR-0032）。読み取り専用の dry-run。"""

    episode_id: str
    resumable: bool
    #: 次に入る工程（``PipelineStage.value``）。resumable が False なら null。
    target_stage: str | None
    #: target_stage から upload までの工程（値の列）。resumable が False でも
    #: target_stage が決まっていれば非空になりうる（参考情報。実行するかは resumable が決める）。
    stages_to_run: list[str]
    #: 再開を止めている理由（人が読める文の列）。空なら resumable。
    unresolved_blockers: list[str]
    #: 成否不明で残っている予約の id（評価は `find_unreconciled` と同じ述語。台帳の詳細はここで
    #: は出さない。secret を含みうる詳細は運用者が台帳を直接見る）。
    unreconciled_reservation_ids: list[str]
    #: 課金が起きうる工程の開示（保証ではない。ResumePlan のドキュメント参照）。
    possible_new_charges: list[str]
    reason: str | None


class ResumeResponse(BaseModel):
    """``POST /episodes/{id}/resume``（ADR-0032）。"""

    episode_id: str
    #: 起動を受け付けた時点の状態。工程に入れたかは workflow の admit が決める。
    status: EpisodeStatus
    workflow_id: str
    target_stage: str
    stages_to_run: list[str]


class JobView(BaseModel):
    id: str
    type: JobType
    status: JobStatus
    attempts: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime


class ArtifactView(BaseModel):
    id: str
    artifact_type: ArtifactType
    schema_version: str
    bucket: str
    object_key: str
    sha256: str
    created_at: datetime


class EpisodeView(BaseModel):
    id: str
    status: EpisodeStatus
    topic: str | None
    created_at: datetime
    updated_at: datetime
    jobs: list[JobView]
    artifacts: list[ArtifactView]
