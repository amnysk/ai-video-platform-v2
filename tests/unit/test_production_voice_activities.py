"""VoiceActivities を Activity 単位で検査する（ADR-0017 / Phase 4B）。

Temporal を介さず直接呼ぶ。SQLite + InMemoryArtifactStore + FakeVoiceGenerator（INV-18）。
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from contracts.artifacts import parse_scene_voice_artifact
from contracts.states import ArtifactType, FailureClass, JobStatus, JobType
from domain.artifact.hashing import sha256_hex
from domain.errors import VoiceLanguageUnsupportedError
from infrastructure.db.repositories import ArtifactMetadataRepository, JobRepository
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeVoiceGenerator
from tests.support.voice import BUCKET, create_episode, record_inputs, voice_request
from workers.production_voice.activities import VoiceActivities, compute_voice_input_hash


@pytest.fixture
def store() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


@pytest.fixture
def workdir(tmp_path) -> WorkDirectory:
    return WorkDirectory(tmp_path / "work")


def make(session_factory, store, workdir, generator=None) -> VoiceActivities:
    return VoiceActivities(
        session_factory=session_factory,
        store=store,
        generator=generator or FakeVoiceGenerator(),
        probe=PillowAvMediaProbe(),
        workdir=workdir,
        bucket=BUCKET,
    )


async def _jobs(session_factory, episode_id):
    async with session_factory() as session:
        return [
            j
            for j in await JobRepository(session).list_for_episode(episode_id)
            if j.type is JobType.PRODUCE_SCENE_VOICE
        ]


async def test_generates_scene_voice_artifact_with_media_and_job(
    session_factory, store, workdir
) -> None:
    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode)
    generator = FakeVoiceGenerator()
    activities = make(session_factory, store, workdir, generator)

    result = await activities.generate_voice(voice_request(episode, script, storyboard, "s2"))

    assert result.reused is False and generator.calls == 1
    artifact = parse_scene_voice_artifact(await store.get_json(result.object_key))
    assert artifact.script_scene_id == "s2"
    assert artifact.storyboard_scene_ids == ("sb2", "sb3")
    assert artifact.source_script.artifact_id == script.id
    assert artifact.source_storyboard.sha256 == storyboard.sha256
    assert artifact.voice_id == "fake-voice-ja" and artifact.language == "ja"
    assert artifact.media.mime == "audio/wav"
    assert artifact.sample_rate_hz == 22050 and artifact.channels == 1
    media = await store.get_bytes(artifact.media.object_key)
    assert sha256_hex(media) == artifact.media.sha256
    assert artifact.media.object_key.startswith(f"media/{episode}/scene_voice/s2/")
    assert "narration" not in (await store.get_json(result.object_key))

    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).find_current_by_type(
            episode, ArtifactType.SCENE_VOICE, scene_id="s2"
        )
    assert meta is not None and meta.id == result.artifact_id
    [job] = await _jobs(session_factory, episode)
    assert job.status is JobStatus.SUCCEEDED and job.scene_id == "s2"
    # 作業領域は片付けられている
    assert not list((workdir.root / "episodes").rglob("*.wav"))


async def test_reuses_existing_artifact_without_synthesizing(
    session_factory, store, workdir
) -> None:
    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode)
    generator = FakeVoiceGenerator()
    activities = make(session_factory, store, workdir, generator)
    request = voice_request(episode, script, storyboard, "s1")

    first = await activities.generate_voice(request)
    second = await activities.generate_voice(request)

    assert second.reused is True and second.artifact_id == first.artifact_id
    assert generator.calls == 1
    statuses = [j.status for j in await _jobs(session_factory, episode)]
    assert statuses == [JobStatus.SUCCEEDED, JobStatus.SKIPPED]


async def test_input_hash_is_stable_and_follows_narration(session_factory, store) -> None:
    generator = FakeVoiceGenerator()
    kwargs: dict[str, Any] = dict(
        episode_id="ep",
        script_sha256="a" * 64,
        storyboard_sha256="b" * 64,
        script_scene_id="s1",
        storyboard_scene_ids=["sb1"],
        narration="hello",
        language="en",
        generator=generator,
    )
    assert compute_voice_input_hash(**kwargs) == compute_voice_input_hash(**kwargs)
    assert compute_voice_input_hash(**kwargs) != compute_voice_input_hash(
        **{**kwargs, "narration": "hello!"}
    )
    assert compute_voice_input_hash(**kwargs) != compute_voice_input_hash(
        **{**kwargs, "language": "ja"}
    )


async def test_unsupported_language_is_non_retryable_and_fails_the_job(
    session_factory, store, workdir
) -> None:
    class EnglishOnly(FakeVoiceGenerator):
        async def synthesize(self, text, language, dest):
            raise VoiceLanguageUnsupportedError(f"voice speaks en, not {language}")

    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode, language="ja")
    activities = make(session_factory, store, workdir, EnglishOnly())

    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(voice_request(episode, script, storyboard, "s1"))
    assert info.value.type == "VoiceLanguageUnsupportedError"
    assert info.value.non_retryable is True
    [job] = await _jobs(session_factory, episode)
    assert job.failure_class is FailureClass.NEEDS_INPUT
    assert job.status is not JobStatus.RETRYABLE_FAILED


async def test_crash_is_retried_on_the_same_job(session_factory, store, workdir) -> None:
    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode)
    generator = FakeVoiceGenerator(fail_times=1)
    activities = make(session_factory, store, workdir, generator)
    request = voice_request(episode, script, storyboard, "s3")

    with pytest.raises(RuntimeError):  # 未分類はそのまま（Temporal が retry する）
        await activities.generate_voice(request)
    [job] = await _jobs(session_factory, episode)
    assert job.status is JobStatus.RETRYABLE_FAILED

    result = await activities.generate_voice(request)
    assert result.reused is False
    [job] = await _jobs(session_factory, episode)
    assert job.status is JobStatus.SUCCEEDED and job.attempts == 2


async def test_invalid_media_is_retryable_application_error(
    session_factory, store, workdir
) -> None:
    class Garbage(FakeVoiceGenerator):
        async def synthesize(self, text, language, dest):
            await dest.write(b"not a wav")

    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode)
    activities = make(session_factory, store, workdir, Garbage())
    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(voice_request(episode, script, storyboard, "s1"))
    assert info.value.type == "MediaValidationError"
    assert info.value.non_retryable is False


async def test_stale_or_missing_inputs_are_needs_input(session_factory, store, workdir) -> None:
    episode = await create_episode(session_factory)
    activities = make(session_factory, store, workdir)
    script, storyboard = await record_inputs(session_factory, store, episode)
    request = voice_request(episode, script, storyboard, "s1")

    # storyboard_scene_ids が現行の storyboard と食い違う
    bad = voice_request(episode, script, storyboard, "s1")
    bad.storyboard_scene_ids = ["sb2"]
    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(bad)
    assert info.value.type == "ProductionInputInvalidError" and info.value.non_retryable

    # 台本が更新され、plan が古い
    await record_inputs(session_factory, store, episode, narrations={"s1": "新しい文。"})
    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(request)
    assert info.value.type == "ProductionInputInvalidError"

    other = await create_episode(session_factory)
    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(voice_request(other, script, storyboard, "s1"))
    assert info.value.type == "ProductionInputMissingError"
    assert await _jobs(session_factory, episode) == []


async def test_sha_mismatch_of_stored_script_is_needs_input(
    session_factory, store, workdir
) -> None:
    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode)
    store._objects[script.object_key] = b'{"tampered": true}'  # noqa: SLF001
    with pytest.raises(ApplicationError) as info:
        await make(session_factory, store, workdir).generate_voice(
            voice_request(episode, script, storyboard, "s1")
        )
    assert info.value.type == "ProductionInputInvalidError"
