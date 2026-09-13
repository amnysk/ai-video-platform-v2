"""Storyboard Worker のエントリポイント（ADR-0015 / ADR-0016）。

**Ubuntu ホストのプロセス**として動く（Codex CLI と OpenMontage checkout がホストにあるため）。
Workerは他のWorkerを呼ばない（INV-3）。次のJobも決めない（INV-4）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import STORYBOARD_WORKFLOW
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.openmontage_storyboard import (
    OpenMontageGuidedStoryboardGenerator,
    load_openmontage_spec,
)
from infrastructure.providers.process import SubprocessRunner
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from prompts import STORYBOARD_PROMPT_TEMPLATE_ID, STORYBOARD_PROMPT_TEMPLATE_VERSION
from workers.storyboard.activities import DEFAULT_MODEL_LABEL, StoryboardActivities
from workers.storyboard.workflows import StoryboardWorkflow

logger = logging.getLogger(__name__)

_, STORYBOARD_TASK_QUEUE = STORYBOARD_WORKFLOW


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    if not settings.openmontage_repo_path:
        # 仕様 blob を読めないまま起動すると、全ラウンドが needs_input で落ちるだけになる。
        sys.exit(
            "OPENMONTAGE_REPO_PATH is not set: storyboard worker needs the OpenMontage "
            "checkout (read-only) to load the pinned generation spec"
        )

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()

    runner = SubprocessRunner()
    spec = await load_openmontage_spec(
        repo_path=settings.openmontage_repo_path,
        commit=settings.openmontage_commit,
        runner=runner,
    )

    binary = resolve_codex_binary(settings.codex_binary)
    workspace = Path(settings.codex_workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    model_label = settings.codex_model or DEFAULT_MODEL_LABEL

    generator = OpenMontageGuidedStoryboardGenerator(
        llm=CodexCliStoryGenerator(
            binary=binary,
            model=settings.codex_model,
            runner=runner,
            workspace=workspace,
        ),
        spec=spec,
        workdir=WorkDirectory(settings.ai_video_work_root),
        model_label=model_label,
    )

    activities = StoryboardActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        generator=generator,
        bucket=settings.minio_bucket,
        timeout_seconds=settings.storyboard_timeout_seconds,
        prompt_template_id=STORYBOARD_PROMPT_TEMPLATE_ID,
        prompt_template_version=STORYBOARD_PROMPT_TEMPLATE_VERSION,
        model_label=model_label,
    )

    logger.info(
        "storyboard worker listening on task queue %s (codex=%s, spec=%s)",
        STORYBOARD_TASK_QUEUE,
        binary,
        generator.generation_spec_id,
    )
    async with Worker(
        client,
        task_queue=STORYBOARD_TASK_QUEUE,
        workflows=[StoryboardWorkflow],
        activities=activities.all_activities(),
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
