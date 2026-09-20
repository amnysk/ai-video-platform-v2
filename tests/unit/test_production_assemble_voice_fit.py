"""マニフェスト組み立てが、音声の実尺と区間の適合を描画の直前に検査する（ADR-0028）。

音声の Artifact は製作時の検査（VoiceActivities）を通ったものだけが現行になる。それでも、
検査を持たなかった版の worker が作った音声、あるいは storyboard だけが差し替わった世代の
音声が残りうる。描画（VoiceTimelineOverflowError）まで持ち越さず、ここで同じ検査をかける。
台本シーンの区間は tests.support.voice.STORYBOARD_LAYOUT で s1=8,000 / s2=9,000 / s3=8,000 ms。
"""

from __future__ import annotations

import pytest

from contracts.artifacts import PRODUCTION_ARTIFACT_SCHEMA_VERSION, build_scene_voice_artifact
from contracts.production_activities import ProductionAssembleRequest
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.errors import ProductionInputMissingError, VoiceExceedsSceneSpanError
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.production import GENERATOR, media_descriptor
from tests.support.voice import BUCKET, STORYBOARD_LAYOUT, create_episode, record_inputs
from workers.production.activities import ProductionActivities

VOICE_SCENES = {"s1": ["sb1"], "s2": ["sb2", "sb3"], "s3": ["sb4"]}


async def _record_voice(factory, store, episode_id, script, storyboard, scene_id, duration_ms):
    payload = build_scene_voice_artifact(
        episode_id=episode_id,
        source_storyboard={
            "artifact_id": storyboard.id,
            "sha256": storyboard.sha256,
            "schema_version": storyboard.schema_version,
        },
        source_script={
            "artifact_id": script.id,
            "sha256": script.sha256,
            "schema_version": script.schema_version,
        },
        script_scene_id=scene_id,
        storyboard_scene_ids=VOICE_SCENES[scene_id],
        language="ja",
        voice_id="v",
        media=media_descriptor("audio/wav", sha256=sha256_hex(scene_id.encode())),
        duration_ms=duration_ms,
        sample_rate_hz=22050,
        channels=1,
        generator=GENERATOR,
    )
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        artifact_object_key(episode_id, ArtifactType.SCENE_VOICE.value, digest, scene_id), payload
    )
    async with factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.SCENE_VOICE,
            schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
            bucket=BUCKET,
            object_key=put.key,
            sha256=digest,
            input_hash=digest,
            scene_id=scene_id,
        )
        await session.commit()


async def _assemble(session_factory, durations: dict[str, int]) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(session_factory, store, episode)
    for scene_id, duration_ms in durations.items():
        await _record_voice(
            session_factory, store, episode, script, storyboard, scene_id, duration_ms
        )
    activities = ProductionActivities(session_factory=session_factory, store=store, bucket=BUCKET)
    await activities.assemble_manifest(
        ProductionAssembleRequest(
            episode_id=episode,
            workflow_id="episode-x-production",
            run_id="run-1",
            storyboard_artifact_id=storyboard.id,
            script_artifact_id=script.id,
        )
    )


async def test_overflowing_voice_stops_assembly_before_render(session_factory) -> None:
    """9/19 型（s1 が 10,147 ms、区間は 8,000 ms）を、描画へ渡す前の入口で止める。"""
    with pytest.raises(VoiceExceedsSceneSpanError) as info:
        await _assemble(session_factory, {"s1": 10147, "s2": 9000, "s3": 1000})
    assert "s1" in str(info.value) and "s3" not in str(info.value)


async def test_voices_that_fit_pass_the_check(session_factory) -> None:
    """区間ちょうどは収まる（描画の重なり判定は `終わり > 次の開始` だけを拒む）。"""
    # 画像・動画が無いので、この検査を通ったあとは既存の欠け検査（needs_input）で止まる
    with pytest.raises(ProductionInputMissingError):
        await _assemble(session_factory, {"s1": 8000, "s2": 9000, "s3": 8000})


def test_layout_matches_the_spans_asserted_above() -> None:
    starts = {sid: start for sid, start, *_ in reversed(STORYBOARD_LAYOUT)}
    assert (starts["s2"] - starts["s1"], starts["s3"] - starts["s2"]) == (8000, 9000)
