"""Topic Planner の worker 登録（ADR-0025）と Analytics provider の組み立て。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from contracts.topic_planning import (
    TOPIC_PLANNER_ACTIVITY_NAMES,
    TOPIC_PLANNER_TASK_QUEUE,
    TOPIC_PLANNER_WORKFLOW,
)
from infrastructure.analytics.youtube_analytics import YouTubeAnalyticsProvider
from infrastructure.config import Settings
from tests.support.fakes import FakeStoryGenerator
from workers.planning import run_worker
from workers.planning.topic_activities import TopicPlannerActivities
from workers.planning.topic_workflows import TopicPlannerWorkflow


def test_all_contract_activity_names_are_registered(session_factory) -> None:
    acts = TopicPlannerActivities(
        session_factory=session_factory,
        generator=FakeStoryGenerator(),
        analytics=None,
        analytics_provider_id="youtube_analytics",
        clock=lambda: datetime.now(UTC),
        timeout_seconds=60,
    )
    names = {fn.__temporal_activity_definition.name for fn in acts.all_activities()}  # type: ignore[attr-defined]
    assert names == set(TOPIC_PLANNER_ACTIVITY_NAMES)


def test_planner_runs_on_the_script_worker_queue() -> None:
    name, queue = TOPIC_PLANNER_WORKFLOW
    assert name == TopicPlannerWorkflow.__temporal_workflow_definition.name  # type: ignore[attr-defined]
    assert queue == TOPIC_PLANNER_TASK_QUEUE == run_worker.SCRIPT_TASK_QUEUE
    assert TopicPlannerWorkflow in run_worker.WORKFLOWS


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in (
        "YOUTUBE_ANALYTICS_ENABLED",
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.asyncio
async def test_analytics_provider_is_built_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async with httpx.AsyncClient() as client:
        assert run_worker.build_analytics_provider(_settings(monkeypatch), client) is None
        # 有効でも OAuth が揃わなければ組まない（Planner は劣化して続ける）
        enabled = _settings(monkeypatch, YOUTUBE_ANALYTICS_ENABLED="true")
        assert run_worker.build_analytics_provider(enabled, client) is None

        token = tmp_path / "refresh-token"
        token.write_text("refresh", encoding="utf-8")
        token.chmod(0o600)
        full = _settings(
            monkeypatch,
            YOUTUBE_ANALYTICS_ENABLED="true",
            YOUTUBE_CLIENT_ID="id",
            YOUTUBE_CLIENT_SECRET="secret",
            YOUTUBE_REFRESH_TOKEN_PATH=str(token),
        )
        provider = run_worker.build_analytics_provider(full, client)
        assert isinstance(provider, YouTubeAnalyticsProvider)
