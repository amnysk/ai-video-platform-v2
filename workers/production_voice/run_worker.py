"""Production Voice Worker のエントリポイント（ADR-0017 / Phase 4B）。

**Ubuntu ホストのプロセス**として動く（Piper の隔離 venv と音声モデルがホストにあるため）。
piper は import しない。隔離 venv の python を子プロセスとして起動する。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import PRODUCTION_VOICE_TASK_QUEUE
from domain.errors import ProviderUnavailableError
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.providers.piper_voice import PiperVoiceGenerator
from infrastructure.providers.process import SubprocessRunner
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from workers.production_voice.activities import VoiceActivities

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    try:
        generator = await PiperVoiceGenerator.load(
            python=settings.piper_python,
            model_path=settings.piper_voice_path,
            runner=SubprocessRunner(),
            timeout_seconds=settings.production_voice_timeout_seconds,
            length_scale=settings.piper_length_scale,
            noise_scale=settings.piper_noise_scale,
            noise_w_scale=settings.piper_noise_w_scale,
        )
    except ProviderUnavailableError as exc:
        # 組めないまま起動すると全 Activity が needs_input で落ちるだけになる。
        sys.exit(f"voice worker cannot start: {exc}")

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()

    activities = VoiceActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        workdir=WorkDirectory(settings.ai_video_work_root),
        bucket=settings.minio_bucket,
    )
    logger.info(
        "voice worker listening on task queue %s (profile=%s, concurrency=%s)",
        PRODUCTION_VOICE_TASK_QUEUE,
        generator.generation_profile_id,
        settings.voice_concurrency,
    )
    async with Worker(
        client,
        task_queue=PRODUCTION_VOICE_TASK_QUEUE,
        activities=activities.all_activities(),
        max_concurrent_activities=settings.voice_concurrency,
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
