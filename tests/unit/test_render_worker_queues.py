"""render worker の queue 分割とストアの流す経路（ADR-0019 §8）。"""

from __future__ import annotations

import pytest

from contracts.render_activities import RENDER_ACTIVITY_NAMES, RENDER_FINAL_VIDEO
from contracts.states import RENDER_MEDIA_TASK_QUEUE, RENDER_TASK_QUEUE
from domain.artifact.hashing import sha256_hex
from infrastructure.storage.artifact_store import ArtifactConflictError, readback_sha256
from infrastructure.storage.memory_store import InMemoryArtifactStore
from workers.render.activities import RenderActivities
from workers.render.workflows import RenderWorkflowInput


def _names(fns) -> set[str]:
    return {fn.__temporal_activity_definition.name for fn in fns}


def test_render_activity_is_alone_on_the_media_queue() -> None:
    assert RENDER_MEDIA_TASK_QUEUE != RENDER_TASK_QUEUE
    assert _names(
        RenderActivities.media_activities(RenderActivities.__new__(RenderActivities))
    ) == {RENDER_FINAL_VIDEO}
    state = _names(RenderActivities.state_activities(RenderActivities.__new__(RenderActivities)))
    assert RENDER_FINAL_VIDEO not in state
    assert state | {RENDER_FINAL_VIDEO} == set(RENDER_ACTIVITY_NAMES)
    assert RenderWorkflowInput(episode_id="e").render_task_queue == RENDER_MEDIA_TASK_QUEUE


def test_build_workers_limits_only_the_media_worker(monkeypatch) -> None:
    import workers.render.run_worker as rw
    from infrastructure.config import Settings

    created: list[dict] = []

    class _Worker:
        def __init__(self, client, **kwargs) -> None:
            created.append(kwargs)

    monkeypatch.setattr(rw, "Worker", _Worker)
    acts = RenderActivities.__new__(RenderActivities)
    rw.build_workers(object(), Settings(render_concurrency=2), acts)  # type: ignore[arg-type]

    state, media = created
    assert state["task_queue"] == RENDER_TASK_QUEUE and "max_concurrent_activities" not in state
    assert state["workflows"] and RENDER_FINAL_VIDEO not in _names(state["activities"])
    assert media["task_queue"] == RENDER_MEDIA_TASK_QUEUE
    assert media["max_concurrent_activities"] == 2
    assert _names(media["activities"]) == {RENDER_FINAL_VIDEO}


async def test_memory_store_streaming_methods(tmp_path) -> None:
    store = InMemoryArtifactStore()
    source = tmp_path / "a.bin"
    source.write_bytes(b"abc" * 1000)
    put = await store.put_file("k", source, "video/mp4")
    assert put.sha256 == sha256_hex(b"abc" * 1000)
    assert await readback_sha256(store, "k") == put.sha256
    assert await store.download_to("k", tmp_path / "b.bin") == put.sha256
    with pytest.raises(KeyError):
        await store.sha256_of("missing")
    other = tmp_path / "c.bin"
    other.write_bytes(b"x")
    with pytest.raises(ArtifactConflictError):
        await store.put_file("k", other, "video/mp4")
