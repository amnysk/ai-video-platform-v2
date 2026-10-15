"""HTTP の ``start_agentic_production`` も worker・CLI と同じ options で起動する（ADR-0044 §3-2）。

理由: docs/testing/jpmas2-p2a-integration-rationale.md
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.api.application_service import LazyAgenticStarter
from infrastructure.config import Settings
from infrastructure.temporal.pipeline_options import pipeline_options_from_settings


class _Client:
    namespace = "avp-test"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, name: str, arg: Any, **kw: Any) -> None:
        self.calls.append({"name": name, "arg": arg, **kw})


@pytest.mark.asyncio
async def test_the_http_starter_uses_the_settings_pipeline_options() -> None:
    settings = Settings(image_concurrency=7, pipeline_render_profile_id="shorts_1080p")
    client = _Client()

    async def connect(_: Settings) -> Any:
        return client

    starter = LazyAgenticStarter(settings, connect=connect)
    await starter.start(episode_id=uuid.uuid4(), template_id="shorts_agentic", template_version="1")

    (call,) = client.calls
    expected = pipeline_options_from_settings(settings)
    assert call["arg"].options == expected
    assert call["task_queue"] == expected.pipeline_task_queue
