"""実PostgreSQL + 実MinIO で VoiceActivities を検査する（ADR-0017 / Phase 4B）。

生成器は FakeVoiceGenerator（本物の wav を返す）。Piper の実合成は tests/live。
共有 DB を壊さないよう一時スキーマに閉じ込める。
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.artifacts import parse_scene_voice_artifact
from contracts.states import ArtifactType, JobStatus, JobType
from infrastructure.db.models import Base
from infrastructure.db.repositories import ArtifactMetadataRepository, JobRepository
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.storage.artifact_store import readback_sha256
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.production import FakeVoiceGenerator
from tests.support.voice import BUCKET, create_episode, record_inputs, voice_request
from workers.production_voice.activities import VoiceActivities

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="TEST_DATABASE_URL (*_test) and MINIO_ENDPOINT must be set",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"voice_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from infrastructure.config import Settings

    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    return store


def _activities(factory, store, tmp_path, generator) -> VoiceActivities:
    return VoiceActivities(
        session_factory=factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        workdir=WorkDirectory(tmp_path / "work"),
        bucket=BUCKET,
    )


async def _current_voice(factory, episode_id, scene_id):
    async with factory() as session:
        return await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCENE_VOICE, scene_id=scene_id
        )


async def test_generate_reuse_and_supersede_within_one_scene(
    pg_session_factory, minio_store, tmp_path
) -> None:
    factory = pg_session_factory
    episode = await create_episode(factory)
    script, storyboard = await record_inputs(factory, minio_store, episode)
    generator = FakeVoiceGenerator()
    activities = _activities(factory, minio_store, tmp_path, generator)

    # 生成: 全台本シーン
    first = {
        sid: await activities.generate_voice(voice_request(episode, script, storyboard, sid))
        for sid in ("s1", "s2", "s3")
    }
    assert generator.calls == 3 and not any(r.reused for r in first.values())
    for result in first.values():
        artifact = parse_scene_voice_artifact(await minio_store.get_json(result.object_key))
        assert await readback_sha256(minio_store, artifact.media.object_key) == (
            artifact.media.sha256
        )
        stat = await minio_store.stat(artifact.media.object_key)
        assert stat.content_type == "audio/wav"

    # 再利用: 合成しない、job は skipped
    again = await activities.generate_voice(voice_request(episode, script, storyboard, "s2"))
    assert again.reused and again.artifact_id == first["s2"].artifact_id
    assert generator.calls == 3
    async with factory() as session:
        jobs = [
            j
            for j in await JobRepository(session).list_for_episode(episode)
            if j.type is JobType.PRODUCE_SCENE_VOICE and j.scene_id == "s2"
        ]
    assert [j.status for j in jobs] == [JobStatus.SUCCEEDED, JobStatus.SKIPPED]

    # 台本の新版で s2 のナレーションだけ変わる
    new_script, new_storyboard = await record_inputs(
        factory, minio_store, episode, narrations={"s2": "土器は煮炊きに使われた。"}
    )
    s2 = await activities.generate_voice(voice_request(episode, new_script, new_storyboard, "s2"))
    assert s2.reused is False and generator.calls == 4
    current_s2 = await _current_voice(factory, episode, "s2")
    assert current_s2 is not None and current_s2.id == s2.artifact_id

    # 他のシーンの現行音声は影響を受けない（台本の sha が変わっても世代は scene 内で閉じる）
    for sid in ("s1", "s3"):
        current = await _current_voice(factory, episode, sid)
        assert current is not None and current.id == first[sid].artifact_id

    async with factory() as session:
        rows = [
            a
            for a in await ArtifactMetadataRepository(session).list_for_episode(episode)
            if a.artifact_type is ArtifactType.SCENE_VOICE
        ]
    assert sorted(a.scene_id or "" for a in rows) == ["s1", "s2", "s2", "s3"]
