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


def test_the_cached_starter_is_per_event_loop() -> None:
    """要求ごとに接続しないが、別の event loop（の client）を跨いで使い回さない。

    process に1つだけ持つと、loop の違う client を握ったまま start が戻らない（E2E の hang）。
    """
    import asyncio

    from apps.api import application_service

    settings = Settings()

    async def get() -> LazyAgenticStarter:
        return application_service.loop_starter(settings)

    async def twice() -> tuple[LazyAgenticStarter, LazyAgenticStarter]:
        return await get(), await get()

    first_a, first_b = asyncio.run(twice())
    second, _ = asyncio.run(twice())
    assert first_a is first_b
    assert second is not first_a
