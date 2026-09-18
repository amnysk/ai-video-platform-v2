"""Script Worker のエントリポイント。

**Ubuntu ホストのプロセス**として動く（Codex CLI がホストにあるため）。
Workerは他のWorkerを呼ばない（INV-3）。次のJobも決めない（INV-4）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
from temporalio.worker import Worker

from contracts.topic_planning import TOPIC_PLANNER_TASK_QUEUE
from domain.topic_planning import AnalyticsProvider
from infrastructure.analytics.youtube_analytics import PROVIDER_ID, YouTubeAnalyticsProvider
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.process import SubprocessRunner
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from infrastructure.youtube.errors import YouTubeAuthError
from infrastructure.youtube.oauth import RefreshTokenCredentials
from workers.planning.activities import ScriptActivities
from workers.planning.topic_activities import TopicPlannerActivities
from workers.planning.topic_workflows import TopicPlannerWorkflow
from workers.planning.workflows import ScriptWorkflow

logger = logging.getLogger(__name__)

#: Topic Planner も同じ queue（Codex を持つこの worker）で動く（ADR-0025）
SCRIPT_TASK_QUEUE = TOPIC_PLANNER_TASK_QUEUE
#: Analytics API の1リクエストの上限（秒）
ANALYTICS_HTTP_TIMEOUT_SECONDS = 30.0

WORKFLOWS = [ScriptWorkflow, TopicPlannerWorkflow]


def build_analytics_provider(
    settings: Settings, client: httpx.AsyncClient
) -> AnalyticsProvider | None:
    """``YOUTUBE_ANALYTICS_ENABLED`` のときだけ組む。

    組めなければ None（Planner は劣化して続ける）。
    """
    if not settings.youtube_analytics_enabled:
        return None
    if not (
        settings.youtube_client_id
        and settings.youtube_client_secret
        and settings.youtube_refresh_token_path
    ):
        logger.warning(
            "YOUTUBE_ANALYTICS_ENABLED but YOUTUBE_CLIENT_ID / _SECRET / _REFRESH_TOKEN_PATH "
            "are not all set; planning without live analytics"
        )
        return None
    try:
        credentials = RefreshTokenCredentials.from_file(
            settings.youtube_client_id,
            settings.youtube_client_secret,
            settings.youtube_refresh_token_path,
            client=client,
        )
    except YouTubeAuthError as exc:
        # メッセージは adapter が token を含めないよう作っている
        logger.warning("youtube analytics disabled: %s", exc)
        return None
    return YouTubeAnalyticsProvider(credentials, client=client)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    client = await connect_with_retry(settings)
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

    session_factory = session_factory_from_settings(settings)
    activities = ScriptActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        bucket=settings.minio_bucket,
        generator_id="codex",
        model=settings.codex_model,
        timeout_seconds=settings.codex_timeout_seconds,
    )

    async with httpx.AsyncClient(timeout=ANALYTICS_HTTP_TIMEOUT_SECONDS) as http:
        analytics = build_analytics_provider(settings, http)
        topic_activities = TopicPlannerActivities(
            session_factory=session_factory,
            generator=generator,
            analytics=analytics,
            analytics_provider_id=PROVIDER_ID,
            clock=lambda: datetime.now(UTC),
            timeout_seconds=settings.codex_timeout_seconds,
        )
        logger.info(
            "script worker listening on task queue %s (codex=%s, live analytics=%s)",
            SCRIPT_TASK_QUEUE,
            binary,
            analytics is not None,
        )
        async with Worker(
            client,
            task_queue=SCRIPT_TASK_QUEUE,
            workflows=WORKFLOWS,
            activities=[*activities.all_activities(), *topic_activities.all_activities()],
        ):
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
