"""brief が変わった後の台本の作り直し（ADR-0044 §3-1）。

workflow が呼ぶ 4 つの Activity を順に呼び、実際の失敗点（成功済みの job への ``start``）から
再現する。各テストの理由: docs/testing/jpmas2-p2a-fixes-rationale.md
"""

from __future__ import annotations

from typing import Any

from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator
from tests.unit.test_script_brief_revision import _write_brief
from tests.unit.test_script_creative_brief import _activities, _plain_episode, _script_json
from workers.planning.activities import CreateJobRequest, EpisodeRef, GenerateScriptRequest


async def _run_script_stage(acts: Any, episode_id: str) -> tuple[str, Any]:
    """``ScriptWorkflow`` と同じ順序で Activity を 1 回通す。"""
    ref = EpisodeRef(episode_id=episode_id)
    await acts.mark_episode_in_progress(ref)
    job_id = await acts.create_script_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    result = await acts.generate_script(
        GenerateScriptRequest(episode_id=episode_id, job_id=job_id, round=1)
    )
    await acts.mark_script_ready(ref)
    return job_id, result


def _numbered_script(n: int) -> str:
    return _script_json().replace("Hidden ruler", f"Hidden ruler {n}")


async def _setup(session_factory: Any) -> tuple[Any, Any, Any, str]:
    store = InMemoryArtifactStore()
    # 呼ぶたびに別の台本にする（同じ内容は同じ Artifact 行に畳まれる: ADR-0012）
    generator = FakeStoryGenerator(output=lambda _r: _numbered_script(len(generator.requests)))
    acts = _activities(session_factory, store, generator)
    episode_id = await _plain_episode(session_factory, "draft")
    await _write_brief(session_factory, store, episode_id)
    return store, generator, acts, episode_id


async def _jobs(session_factory: Any, episode_id: str) -> list[Any]:
    async with session_factory() as s:
        return [
            j
            for j in await JobRepository(s).list_for_episode(episode_id)
            if j.type.value == "write_script"
        ]


async def test_a_changed_brief_after_a_successful_script_gets_a_new_job_and_script(
    session_factory,
) -> None:
    store, generator, acts, episode_id = await _setup(session_factory)
    first_job, first = await _run_script_stage(acts, episode_id)

    await _write_brief(session_factory, store, episode_id, hook="A different opening line here.")
    second_job, second = await _run_script_stage(acts, episode_id)

    assert second_job != first_job
    assert second.reused is False and second.artifact_id != first.artifact_id
    assert len(generator.requests) == 2
    jobs = await _jobs(session_factory, episode_id)
    assert len(jobs) == 2
    assert {j.id for j in jobs} == {first_job, second_job}


async def test_old_script_is_kept_as_history_and_new_one_is_current(session_factory) -> None:
    store, _generator, acts, episode_id = await _setup(session_factory)
    _job, first = await _run_script_stage(acts, episode_id)
    await _write_brief(session_factory, store, episode_id, hook="A different opening line here.")
    _job2, second = await _run_script_stage(acts, episode_id)

    async with session_factory() as s:
        repo = ArtifactMetadataRepository(s)
        scripts = [
            a
            for a in await repo.list_for_episode(episode_id)
            if a.artifact_type is ArtifactType.SCRIPT
        ]
        current = await repo.find_current_by_type(episode_id, ArtifactType.SCRIPT)
    assert {a.id for a in scripts} == {first.artifact_id, second.artifact_id}
    assert current is not None and current.id == second.artifact_id
    old = next(a for a in scripts if a.id == first.artifact_id)
    assert old.version == 1 and current.version == 2  # 旧版は履歴に残る
    assert await store.get_json(old.object_key)  # 旧版の本体も読める


async def test_an_activity_retry_returns_the_new_queued_job(session_factory) -> None:
    store, _generator, acts, episode_id = await _setup(session_factory)
    await _run_script_stage(acts, episode_id)
    await _write_brief(session_factory, store, episode_id, hook="A different opening line here.")

    request = CreateJobRequest(episode_id=episode_id, max_attempts=3)
    one = await acts.create_script_job(request)
    again = await acts.create_script_job(request)  # Activity の再試行
    assert one == again
    assert len(await _jobs(session_factory, episode_id)) == 2


async def test_an_unchanged_brief_reuses_the_script_without_a_new_job(session_factory) -> None:
    _store, generator, acts, episode_id = await _setup(session_factory)
    first_job, _ = await _run_script_stage(acts, episode_id)
    job, result = await _run_script_stage(acts, episode_id)
    assert job == first_job and result.reused is True
    assert len(generator.requests) == 1
    assert len(await _jobs(session_factory, episode_id)) == 1


async def test_reservation_follows_the_brief(session_factory) -> None:
    store, generator, acts, episode_id = await _setup(session_factory)
    await _run_script_stage(acts, episode_id)

    async def reservations() -> list[Any]:
        async with session_factory() as s:
            return await ProviderReservationRepository(s).list_for_episode_provider(
                episode_id, ProviderCall.CODEX_SCRIPT
            )

    assert len(await reservations()) == 1
    await _write_brief(session_factory, store, episode_id, hook="A different opening line here.")
    await _run_script_stage(acts, episode_id)
    assert len(await reservations()) == 2  # 新しい brief は新しい予約
    await _run_script_stage(acts, episode_id)  # 同じ brief の再実行
    assert len(await reservations()) == 2  # 二重課金なし
    assert len(generator.requests) == 2


async def test_the_same_job_is_reused_when_the_latest_one_is_still_open(session_factory) -> None:
    """非終端の job があるなら新しく作らない（途中で落ちた実行の再開）。"""
    _store, _generator, acts, episode_id = await _setup(session_factory)
    request = CreateJobRequest(episode_id=episode_id, max_attempts=3)
    one = await acts.create_script_job(request)
    assert await acts.create_script_job(request) == one


async def test_a_legacy_episode_keeps_returning_the_first_job(session_factory) -> None:
    """brief の無い Episode は従来どおり ``existing[0]``（INV-55）。"""
    store = InMemoryArtifactStore()
    acts = _activities(session_factory, store, FakeStoryGenerator(output=_script_json()))
    episode_id = await _plain_episode(session_factory)
    first_job, _ = await _run_script_stage(acts, episode_id)
    async with session_factory() as s:
        assert (await EpisodeRepository(s).get(episode_id)) is not None
    second = await acts.create_script_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    assert second == first_job


async def test_script_stage_reruns_when_the_episode_has_moved_past_script_ready(
    session_factory,
) -> None:
    """台本の後の駐機点（storyboard_ready）にいる Episode でも、brief 変更後に台本を作り直せる。"""
    store, generator, acts, episode_id = await _setup(session_factory)
    await _run_script_stage(acts, episode_id)
    async with session_factory() as s:
        episodes = EpisodeRepository(s)
        await episodes.apply_event(episode_id, EpisodeEvent.STAGE_ADMITTED)
        await episodes.apply_event(episode_id, EpisodeEvent.STORYBOARD_READY)
        await s.commit()

    await _write_brief(session_factory, store, episode_id, hook="A different opening line here.")
    _job, result = await _run_script_stage(acts, episode_id)
    assert result.reused is False and len(generator.requests) == 2
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(episode_id)
    assert episode is not None and episode.status is EpisodeStatus.SCRIPT_READY


async def test_a_legacy_episode_past_script_ready_is_still_rejected(session_factory) -> None:
    """brief の無い Episode の遷移は従来どおり（遷移表を緩めない / INV-55）。"""
    import pytest

    from domain.errors import InvalidTransitionError

    acts = _activities(
        session_factory, InMemoryArtifactStore(), FakeStoryGenerator(output=_script_json())
    )
    episode_id = await _plain_episode(session_factory)
    await _run_script_stage(acts, episode_id)
    async with session_factory() as s:
        episodes = EpisodeRepository(s)
        await episodes.apply_event(episode_id, EpisodeEvent.STAGE_ADMITTED)
        await episodes.apply_event(episode_id, EpisodeEvent.STORYBOARD_READY)
        await s.commit()
    with pytest.raises(InvalidTransitionError):
        await acts.mark_episode_in_progress(EpisodeRef(episode_id=episode_id))
