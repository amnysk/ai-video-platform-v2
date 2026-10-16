"""本物の名前で登録する fake の工程 workflow と、その状態 Activity。

- 工程は駐機点の status を返す（pipeline はこれしか見ない）
- ``DURABILITY_BLOCK_STAGES`` の工程は signal ``release`` まで待つ（kill を工程の途中に落とす）
- Production は durable timer（``workflow.sleep``）を挟む
- Render は Episode を render_ready にし、現行 final_video のメタデータを置く（upload_gate の要件）
- Upload は Episode を uploaded にする

Episode 状態の変更は Activity（DB = 一時スキーマ）。冪等（再試行・再実行で二重に遷移しない）。
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from tests.support.durability.common import ENV_BLOCK_STAGES

ADVANCE_ACTIVITY = "durability_advance_episode"

BLOCK_STAGES: frozenset[str] = frozenset(
    s for s in os.environ.get(ENV_BLOCK_STAGES, "").split(",") if s
)

PARKING = {
    "ScriptWorkflow": "script_ready",
    "StoryboardWorkflow": "storyboard_ready",
    "ProductionWorkflow": "assets_ready",
    "RenderWorkflow": "render_ready",
    "UploadWorkflow": "uploaded",
}


class _Stage:
    name = ""

    def __init__(self) -> None:
        self._released = False

    @workflow.signal(name="release")
    def release(self) -> None:
        self._released = True

    async def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        ep = payload["episode_id"]
        if self.name in BLOCK_STAGES:
            await workflow.wait_condition(lambda: self._released)
        if self.name == "ProductionWorkflow":
            await workflow.sleep(timedelta(seconds=1))
        if self.name in {"RenderWorkflow", "UploadWorkflow"}:
            await workflow.execute_activity(
                ADVANCE_ACTIVITY,
                args=[ep, PARKING[self.name]],
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200)),
            )
        return {"episode_id": ep, "status": PARKING[self.name]}


@workflow.defn(name="ScriptWorkflow")
class FakeScript(_Stage):
    name = "ScriptWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


@workflow.defn(name="StoryboardWorkflow")
class FakeStoryboard(_Stage):
    name = "StoryboardWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


@workflow.defn(name="ProductionWorkflow")
class FakeProduction(_Stage):
    name = "ProductionWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


@workflow.defn(name="RenderWorkflow")
class FakeRender(_Stage):
    name = "RenderWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


@workflow.defn(name="UploadWorkflow")
class FakeUpload(_Stage):
    name = "UploadWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


FAKE_STAGES = [FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload]


def advance_activity(session_factory: Any) -> Any:
    from contracts.states import ArtifactType, EpisodeStatus
    from domain.episode.transitions import EpisodeEvent
    from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository

    to_render_ready = [
        EpisodeEvent.WORKFLOW_STARTED,
        EpisodeEvent.SCRIPT_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.STORYBOARD_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.ASSETS_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.RENDER_READY,
    ]

    @activity.defn(name=ADVANCE_ACTIVITY)
    async def advance(episode_id: str, target: str) -> str:
        async with session_factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(episode_id)
            assert episode is not None, episode_id
            if target == EpisodeStatus.RENDER_READY.value:
                if episode.status is EpisodeStatus.PLANNED:
                    for event in to_render_ready:
                        await episodes.apply_event(episode_id, event)
                    await ArtifactMetadataRepository(session).record(
                        episode_id=episode_id,
                        artifact_type=ArtifactType.FINAL_VIDEO,
                        schema_version="durability-fake",
                        bucket="artifacts",
                        object_key=f"durability/{episode_id}/final.mp4",
                        sha256="d" * 64,
                    )
            elif episode.status is EpisodeStatus.RENDER_READY:
                await episodes.apply_event(episode_id, EpisodeEvent.STAGE_ADMITTED)
                await episodes.apply_event(episode_id, EpisodeEvent.UPLOAD_SUCCEEDED)
            await session.commit()
            final = await episodes.get(episode_id)
        assert final is not None
        return final.status.value

    return advance
