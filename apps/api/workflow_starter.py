"""Temporal workflow の起動だけを担う薄い層。

APIは workflow の**完了を待たない**（INV-16）。start して即座に返す。
"""

from __future__ import annotations

from typing import Protocol

from temporalio.client import Client

from contracts.pipeline import EPISODE_PIPELINE_WORKFLOW, pipeline_workflow_id
from contracts.production_activities import (
    DEFAULT_AWAIT_REEXECUTIONS,
    DEFAULT_IMAGE_MAX_ROUNDS,
    DEFAULT_VIDEO_MAX_ROUNDS,
)
from contracts.render import DEFAULT_RENDER_PROFILE_ID
from contracts.states import (
    PIPELINE_WORKFLOWS,
    PRODUCTION_WORKFLOW,
    RENDER_WORKFLOW,
    STORYBOARD_WORKFLOW,
    UPLOAD_WORKFLOW,
    Pipeline,
)
from infrastructure.config import Settings


class WorkflowStarter(Protocol):
    async def start_episode_workflow(
        self, *, episode_id: str, pipeline: Pipeline | str = Pipeline.SKELETON
    ) -> str: ...

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        """既存 Episode に対して StoryboardWorkflow を起動する（ADR-0015）。"""
        ...

    async def start_production_workflow(self, *, episode_id: str) -> str:
        """既存 Episode に対して ProductionWorkflow を起動する（ADR-0017）。"""
        ...

    async def start_render_workflow(
        self, *, episode_id: str, render_profile_id: str = DEFAULT_RENDER_PROFILE_ID
    ) -> str:
        """既存 Episode に対して RenderWorkflow を起動する（ADR-0019）。"""
        ...

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        """既存 Episode に対して UploadWorkflow を起動する（ADR-0020）。"""
        ...

    async def start_pipeline_workflow(self, *, episode_id: str, start_stage: str) -> str:
        """既存 Episode の統一再開（ADR-0032）。``EpisodePipelineWorkflow`` を途中入場で起動する。

        id は Episode 作成時の pipeline と同じ規約（``pipeline_workflow_id``）。実行中の同じ id は
        Temporal が拒否する（``WorkflowAlreadyStartedError``。二重再開の防止はここに依存する）。
        """
        ...


class TemporalWorkflowStarter:
    def __init__(self, client: Client, task_queue: str, settings: Settings | None = None) -> None:
        self._client = client
        #: production の workflow 側の並行枠（worker の並行数設定と揃える / ADR-0017）
        self._settings = settings or Settings()
        #: 骨組みworkflowの既定 queue。他は PIPELINE_WORKFLOWS から引く。
        self._task_queue = task_queue

    @classmethod
    async def connect(cls, settings: Settings) -> TemporalWorkflowStarter:
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        return cls(client, settings.temporal_task_queue, settings)

    async def start_episode_workflow(
        self, *, episode_id: str, pipeline: Pipeline | str = Pipeline.SKELETON
    ) -> str:
        selected = Pipeline(pipeline)
        workflow_name, task_queue = PIPELINE_WORKFLOWS[selected]
        if selected is Pipeline.SKELETON:
            task_queue = self._task_queue
        workflow_id = f"episode-{episode_id}"
        # workflow定義そのものはworkerが持つ。APIは名前と引数だけを知る（INV-1 / INV-3）。
        # **完了は待たない**（INV-16）。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        workflow_name, task_queue = STORYBOARD_WORKFLOW
        workflow_id = storyboard_workflow_id(episode_id)
        # 同じ id の実行が走っていれば Temporal が拒否する（二重起動しない）。
        # 完了済みの id は再利用できる（再実行は activity 側の skip 判定で冪等 / INV-17）。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_production_workflow(self, *, episode_id: str) -> str:
        workflow_name, task_queue = PRODUCTION_WORKFLOW
        workflow_id = production_workflow_id(episode_id)
        # 入力は workflow の dataclass と同じ形の dict（worker の型を import しない / INV-3）。
        await self._client.start_workflow(
            workflow_name,
            {
                "episode_id": episode_id,
                "image_concurrency": self._settings.image_concurrency,
                "video_concurrency": self._settings.video_concurrency,
                "voice_concurrency": self._settings.voice_concurrency,
                "image_max_rounds": getattr(
                    self._settings, "production_image_max_rounds", DEFAULT_IMAGE_MAX_ROUNDS
                ),
                "video_max_rounds": getattr(
                    self._settings, "production_video_max_rounds", DEFAULT_VIDEO_MAX_ROUNDS
                ),
                "await_reexecutions": getattr(
                    self._settings, "production_await_reexecutions", DEFAULT_AWAIT_REEXECUTIONS
                ),
            },
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_render_workflow(
        self, *, episode_id: str, render_profile_id: str = DEFAULT_RENDER_PROFILE_ID
    ) -> str:
        workflow_name, task_queue = RENDER_WORKFLOW
        workflow_id = render_workflow_id(episode_id)
        # 入力は RenderWorkflowInput と同じ形の dict（worker の型を import しない / INV-3）。
        # エンジンの timeout は渡さない（worker 側の設定を admit が返す）。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id, "render_profile_id": render_profile_id},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        workflow_name, task_queue = UPLOAD_WORKFLOW
        workflow_id = upload_workflow_id(episode_id)
        # 入力は UploadWorkflowInput と同じ形の dict。公開範囲などの投稿設定は渡さない
        # （private は契約で固定 / INV-19）。同じ id が走っていれば Temporal が拒否する。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_pipeline_workflow(self, *, episode_id: str, start_stage: str) -> str:
        workflow_name, task_queue = EPISODE_PIPELINE_WORKFLOW
        workflow_id = pipeline_workflow_id(episode_id)
        # 入力は EpisodePipelineInput と同じ形の dict（worker の型を import しない / INV-3）。
        # options は渡さない: EpisodePipelineInput.options の default_factory が
        # PipelineOptions() を補う（並行数・render profile 等は各工程の既存 admit/Activity が
        # 個別 POST と同じく既定値・設定から解決する。並べ替えない、AGENTS §8）。
        # id_reuse_policy は既定の ALLOW_DUPLICATE のまま: 元の pipeline 実行は Temporal 上
        # COMPLETED（アプリの outcome=stopped）で終わっているため、ALLOW_DUPLICATE_FAILED_ONLY
        # （DailyEpisodeWorkflow が子を起動するときに使う設定）だと再開を拒否してしまう。
        # 実行中の同じ id は ALLOW_DUPLICATE でも Temporal が構造的に拒否する
        # （二重再開の防止はここに依存する。ADR-0032 §Decision(2)）。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id, "start_stage": start_stage},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id


def upload_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-upload"


def render_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-render"


def production_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-production"


def storyboard_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-storyboard"
