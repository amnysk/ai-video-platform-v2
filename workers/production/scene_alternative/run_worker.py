"""拒否されたシーンの代替案 planner worker のエントリポイント（ADR-0035）。

Codex CLI を使うので、storyboard worker と同じ構成（Codex と認証がある環境）で動かす。
compose service ``scene-alternative-worker`` として常駐させる（ADR-0035 (6)）。
task queue ``production-scene-alternative`` に代替案の計画 Activity だけを登録する。
有料の画像・動画は呼ばない。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from temporalio.worker import Worker

from contracts.production_activities import (
    CODEX_DEFAULT_MODEL_LABEL,
    SCENE_ALTERNATIVE_TASK_QUEUE,
)
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.codex_scene_alternative import CodexSceneAlternativePlanner
from infrastructure.providers.process import SubprocessRunner
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.production.scene_recovery_activities import SceneAlternativeActivities

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()

    binary = resolve_codex_binary(settings.codex_binary)
    workspace = Path(settings.codex_workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    planner = CodexSceneAlternativePlanner(
        llm=CodexCliStoryGenerator(
            binary=binary,
            model=settings.codex_model,
            runner=SubprocessRunner(),
            workspace=workspace,
        ),
        model_label=settings.codex_model or CODEX_DEFAULT_MODEL_LABEL,
        timeout_seconds=settings.codex_timeout_seconds,
    )
    activities = SceneAlternativeActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        bucket=settings.minio_bucket,
        planner=planner,
        # 上限は設定値（ADR-0035 (8)、INV-34）。既定は contracts の1箇所
        max_alternatives_per_scene=settings.production_max_scene_alternatives_per_scene,
        max_alternatives_per_episode=settings.production_max_scene_alternatives_per_episode,
        max_recovery_cost_usd=settings.production_max_recovery_cost_usd,
    )
    logger.info(
        "scene alternative worker listening on task queue %s (codex=%s, profile=%s)",
        SCENE_ALTERNATIVE_TASK_QUEUE,
        binary,
        planner.generation_profile_id,
    )
    async with Worker(
        client,
        task_queue=SCENE_ALTERNATIVE_TASK_QUEUE,
        activities=activities.all_activities(),
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
