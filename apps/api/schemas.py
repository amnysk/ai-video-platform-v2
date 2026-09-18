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
