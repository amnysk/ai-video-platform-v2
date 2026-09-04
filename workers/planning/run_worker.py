"""Script Worker のエントリポイント。

**Ubuntu ホストのプロセス**として動く（Codex CLI がホストにあるため）。
Workerは他のWorkerを呼ばない（INV-3）。次のJobも決めない（INV-4）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.process import SubprocessRunner
from infrastructure.storage.minio_store import MinioArtifactStore
from workers.planning.activities import ScriptActivities
from workers.planning.workflows import ScriptWorkflow

logger = logging.getLogger(__name__)

SCRIPT_TASK_QUEUE = "script"


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()

    binary = resolve_codex_binary(settings.codex_binary)
    workspace = Path(settings.codex_workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)

    generator = CodexCliStoryGenerator(
        binary=binary,
        model=settings.codex_model,
        runner=SubprocessRunner(),
        workspace=workspace,
    )

    activities = ScriptActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        generator=generator,
        bucket=settings.minio_bucket,
        generator_id="codex",
        model=settings.codex_model,
        timeout_seconds=settings.codex_timeout_seconds,
    )

    logger.info("script worker listening on task queue %s (codex=%s)", SCRIPT_TASK_QUEUE, binary)
    async with Worker(
        client,
        task_queue=SCRIPT_TASK_QUEUE,
        workflows=[ScriptWorkflow],
        activities=activities.all_activities(),
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
