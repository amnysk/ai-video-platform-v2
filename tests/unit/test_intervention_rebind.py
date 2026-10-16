"""storyboard を構成の同じ新台本へ結び直す（``revision_links(kind=rebound_to)``、ADR-0041 §6）。

production / voice / render / scene_recovery の「この storyboard が使う台本」は
``infrastructure.intervention.rebind.bound_script_ref`` だけを通る。結び直しが無ければ従来どおり
``storyboard.source_script``。SQLite + メモリストア + fake 生成器（有料 API は呼ばない）。
理由は docs/testing/intervention-d-rationale.md §3。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from contracts.artifacts import (
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    build_script_artifact,
    parse_scene_voice_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.intervention import RevisionLinkKind
from contracts.production_activities import ProductionPlanRequest
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.errors import ProductionInputInvalidError
from domain.production.manifest import ArtifactRef
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.intervention.rebind import ScriptRef, bound_script_ref, check_rebound_script
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.production import SCRIPT_SCENES, FakeImageGenerator, FakeVoiceGenerator
from tests.support.voice import BUCKET, create_episode, record_inputs, voice_request
from workers.production.activities import ProductionActivities

EDITED_S2 = "土器の内側に残った焦げが、煮炊きの証拠になる。"


async def _record_script(
    factory, store, episode_id, scenes: list[dict[str, Any]]
) -> ArtifactMetadata:
    """編集後の台本を現行として記録する（旧台本は superseded になる）。"""
    from tests.support.voice import _record

    return await _record(
        factory,
        store,
        episode_id,
        ArtifactType.SCRIPT,
        SCRIPT_ARTIFACT_SCHEMA_VERSION,
        build_script_artifact(
            episode_id=episode_id,
            language="ja",
            title="縄文の食",
            hook="土器が語る",
            scenes=scenes,
            metadata={"topic": "縄文", "generator": "fake", "generator_model": "fake"},
        ),
    )


def _edited(**changes: Any) -> list[dict[str, Any]]:
    scenes = [dict(s) for s in SCRIPT_SCENES]
    for scene in scenes:
        if scene["id"] == "s2":
            scene.update({"narration": EDITED_S2, **changes})
    return scenes


async def _rebind(factory, episode_id: str, script_id: str, storyboard_id: str) -> None:
    async with factory() as session:
        await InterventionStore(session).add_link(
            episode_id, script_id, storyboard_id, RevisionLinkKind.REBOUND_TO
        )
        await session.commit()


async def _storyboard(factory, store, meta: ArtifactMetadata):
    return parse_storyboard_artifact(await store.get_json(meta.object_key))


# --------------------------------------------------------------------------- 解決


async def test_without_a_link_the_storyboard_source_script_is_used(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    script, storyboard_meta = await record_inputs(session_factory, store, episode)
    storyboard = await _storyboard(session_factory, store, storyboard_meta)
    async with session_factory() as session:
        ref = await bound_script_ref(session, storyboard_meta, storyboard)
    assert ref == ScriptRef(artifact_id=script.id, sha256=script.sha256, rebound=False)


async def test_the_latest_rebound_link_wins(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    _, storyboard_meta = await record_inputs(session_factory, store, episode)
    storyboard = await _storyboard(session_factory, store, storyboard_meta)
    second = await _record_script(session_factory, store, episode, _edited())
    await _rebind(session_factory, episode, second.id, storyboard_meta.id)
    third = await _record_script(session_factory, store, episode, _edited(visual="炉の跡"))
    await _rebind(session_factory, episode, third.id, storyboard_meta.id)
    async with session_factory() as session:
        ref = await bound_script_ref(session, storyboard_meta, storyboard)
    assert ref == ScriptRef(artifact_id=third.id, sha256=third.sha256, rebound=True)


async def test_a_link_to_another_episodes_script_is_rejected(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    other = await create_episode(session_factory)
    _, storyboard_meta = await record_inputs(session_factory, store, episode)
    foreign, _ = await record_inputs(session_factory, store, other)
    storyboard = await _storyboard(session_factory, store, storyboard_meta)
    await _rebind(session_factory, episode, foreign.id, storyboard_meta.id)
    async with session_factory() as session:
        with pytest.raises(ProductionInputInvalidError, match="another episode"):
            await bound_script_ref(session, storyboard_meta, storyboard)


async def test_a_link_to_a_non_script_is_rejected(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    _, storyboard_meta = await record_inputs(session_factory, store, episode)
    storyboard = await _storyboard(session_factory, store, storyboard_meta)
    await _rebind(session_factory, episode, storyboard_meta.id, storyboard_meta.id)
    async with session_factory() as session:
        with pytest.raises(ProductionInputInvalidError, match="not a script"):
            await bound_script_ref(session, storyboard_meta, storyboard)


def _script(scenes: list[dict[str, Any]]):
    return parse_script_artifact(
        build_script_artifact(
            episode_id="ep",
            language="ja",
            title="縄文の食",
            hook="土器が語る",
            scenes=scenes,
            metadata={"topic": "縄文", "generator": "fake", "generator_model": "fake"},
        )
    )


def test_structure_check_accepts_text_only_edits_and_rejects_layout_changes() -> None:
    original = _script([dict(s) for s in SCRIPT_SCENES])
    check_rebound_script(original, _script(_edited()))  # ナレーション・映像の文だけ
    with pytest.raises(ProductionInputInvalidError, match="structure"):
        check_rebound_script(original, _script(_edited(duration_ms=9500)))
    reordered = [dict(SCRIPT_SCENES[1]), dict(SCRIPT_SCENES[0]), dict(SCRIPT_SCENES[2])]
    with pytest.raises(ProductionInputInvalidError, match="structure"):
        check_rebound_script(original, _script(reordered))
    with pytest.raises(ProductionInputInvalidError, match="structure"):
        extra = {"id": "s4", "narration": "追加。", "visual": "追加", "duration_ms": 3000}
        check_rebound_script(original, _script([*(dict(s) for s in SCRIPT_SCENES), extra]))


# --------------------------------------------------------------------------- production


async def test_production_plan_follows_the_rebound_script(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    original, storyboard_meta = await record_inputs(session_factory, store, episode)
    activities = ProductionActivities(session_factory=session_factory, store=store, bucket=BUCKET)
    request = ProductionPlanRequest(episode_id=episode, workflow_id="wf", run_id="r")

    before = await activities.plan(request)
    edited = await _record_script(session_factory, store, episode, _edited())
    legacy = await activities.plan(request)  # 結び直し無し: 従来どおり storyboard の台本
    await _rebind(session_factory, episode, edited.id, storyboard_meta.id)
    after = await activities.plan(request)

    assert before.script_artifact_id == legacy.script_artifact_id == original.id
    assert after.script_artifact_id == edited.id and after.script_sha256 == edited.sha256
    # storyboard は作り直していない。画像・動画の作業は同じ storyboard の同じシーン
    assert after.storyboard_artifact_id == before.storyboard_artifact_id == storyboard_meta.id
    assert after.images == before.images and after.videos == before.videos


async def test_production_plan_refuses_a_rebound_script_with_another_layout(
    session_factory,
) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    _, storyboard_meta = await record_inputs(session_factory, store, episode)
    changed = await _record_script(session_factory, store, episode, _edited(duration_ms=9500))
    await _rebind(session_factory, episode, changed.id, storyboard_meta.id)
    activities = ProductionActivities(session_factory=session_factory, store=store, bucket=BUCKET)
    with pytest.raises(ProductionInputInvalidError, match="structure"):
        await activities.plan(
            ProductionPlanRequest(episode_id=episode, workflow_id="w", run_id="r")
        )


# --------------------------------------------------------------------------- voice / image


def _voice_activities(session_factory, store, tmp_path, generator):
    from infrastructure.media.probe import PillowAvMediaProbe
    from infrastructure.workdir import WorkDirectory
    from workers.production_voice.activities import VoiceActivities

    return VoiceActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        workdir=WorkDirectory(tmp_path / "voice"),
        bucket=BUCKET,
    )


def _image_activities(session_factory, store, tmp_path, generator):
    from infrastructure.media.probe import PillowAvMediaProbe
    from infrastructure.production.paid_job import PaidJobRunner
    from infrastructure.workdir import WorkDirectory
    from workers.production_image.activities import ImageProductionActivities

    runner = PaidJobRunner(
        session_factory=session_factory,
        store=store,
        workdir=WorkDirectory(tmp_path / "image", forbidden=()),
    )
    return ImageProductionActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        runner=runner,
        bucket=BUCKET,
        poll_interval_seconds=0,
    )


async def _produce_image(acts, episode_id: str, storyboard_id: str, scene: str):
    from contracts.production_activities import ImageAwaitRequest, ImageSubmitRequest

    submitted = await acts.submit(
        ImageSubmitRequest(
            episode_id=episode_id,
            workflow_id="wf",
            run_id="run",
            scene_id=scene,
            storyboard_artifact_id=storyboard_id,
            round=1,
        )
    )
    if submitted.artifact is not None:
        return submitted.artifact
    return await acts.await_image(
        ImageAwaitRequest(
            episode_id=episode_id,
            workflow_id="wf",
            run_id="run",
            scene_id=scene,
            storyboard_artifact_id=storyboard_id,
            reservation_id=submitted.reservation_id,
        )
    )


async def _reservations(session_factory, episode_id: str) -> int:
    """この Episode の有料予約の数（全 provider）。"""
    from sqlalchemy import func, select

    from infrastructure.db.models import ProviderReservationRow

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ProviderReservationRow)
            .where(ProviderReservationRow.episode_id == uuid.UUID(episode_id))
        )
    return int(count or 0)


async def test_narration_edit_regenerates_voice_but_reuses_paid_images(
    session_factory, tmp_path
) -> None:
    """設計 §17-7 / ADR-0041 §6: ナレーション修正は音声だけ作り直し、有料の画像を再利用する。"""
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    original, storyboard_meta = await record_inputs(session_factory, store, episode)
    images = FakeImageGenerator()
    voices = FakeVoiceGenerator()
    image_acts = _image_activities(session_factory, store, tmp_path, images)
    voice_acts = _voice_activities(session_factory, store, tmp_path, voices)

    first_image = await _produce_image(image_acts, episode, storyboard_meta.id, "sb2")
    first_voice = await voice_acts.generate_voice(
        voice_request(episode, original, storyboard_meta, "s2")
    )
    paid_before = await _reservations(session_factory, episode)
    submits_before = images.submit_calls

    edited = await _record_script(session_factory, store, episode, _edited())
    await _rebind(session_factory, episode, edited.id, storyboard_meta.id)

    again_image = await _produce_image(image_acts, episode, storyboard_meta.id, "sb2")
    new_voice = await voice_acts.generate_voice(
        voice_request(episode, edited, storyboard_meta, "s2")
    )

    # 画像: 入力指紋は storyboard だけに依存するので同じ。新しい予約（課金）を作らない
    assert again_image.artifact_id == first_image.artifact_id
    assert await _reservations(session_factory, episode) == paid_before
    assert images.submit_calls == submits_before == 1
    assert again_image.reused is True
    # 音声: 新しい台本のナレーションから作り直す（旧台本の音声は再利用しない）
    assert new_voice.reused is False and new_voice.artifact_id != first_voice.artifact_id
    artifact = parse_scene_voice_artifact(await store.get_json(new_voice.object_key))
    assert artifact.source_script.artifact_id == edited.id
    assert artifact.source_storyboard.artifact_id == storyboard_meta.id
    assert voices.calls == 2


async def test_voice_without_a_link_still_refuses_a_storyboard_from_an_older_script(
    session_factory, tmp_path
) -> None:
    """従来どおり: 結び直しが無ければ、古い台本の storyboard で新しい台本の音声を作らない。"""
    from temporalio.exceptions import ApplicationError

    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    _, storyboard_meta = await record_inputs(session_factory, store, episode)
    edited = await _record_script(session_factory, store, episode, _edited())
    voice_acts = _voice_activities(session_factory, store, tmp_path, FakeVoiceGenerator())
    with pytest.raises(ApplicationError) as info:
        await voice_acts.generate_voice(voice_request(episode, edited, storyboard_meta, "s2"))
    assert info.value.type == "ProductionInputInvalidError"
    assert "was not made from current script" in str(info.value)


# --------------------------------------------------------------------------- render（純粋関数）


def _rebound_render_inputs():
    """storyboard は旧台本 S1 から、マニフェスト・音声は結び直した S2 から作られた入力一式。"""
    from domain.production.manifest import build_manifest
    from domain.render.plan import Pinned
    from tests.support.render_fixtures import render_inputs

    base = render_inputs()
    s2 = base.script.artifact.model_copy(
        update={
            "scenes": tuple(
                s.model_copy(update={"narration": EDITED_S2}) if s.id == "s2" else s
                for s in base.script.artifact.scenes
            )
        }
    )
    from domain.artifact.hashing import canonical_json_bytes, sha256_hex

    s2_ref = ArtifactRef(
        str(uuid.uuid4()), sha256_hex(canonical_json_bytes(s2.model_dump(mode="json")))
    )
    voices = {}
    for sid, pinned in base.voices.items():
        payload = pinned.artifact.model_dump(mode="json")
        payload["source_script"] = {
            "artifact_id": s2_ref.artifact_id,
            "sha256": s2_ref.sha256,
            "schema_version": "1.0",
        }
        voice = parse_scene_voice_artifact(payload)
        voices[sid] = Pinned(
            artifact=voice,
            ref=ArtifactRef(str(uuid.uuid4()), sha256_hex(canonical_json_bytes(payload))),
        )
    from contracts.artifacts import parse_production_manifest

    manifest = parse_production_manifest(
        build_manifest(
            episode_id=base.manifest.episode_id,
            storyboard_ref=base.storyboard.ref,
            script_ref=s2_ref,
            storyboard=base.storyboard.artifact,
            script=s2,
            images={
                s.scene_id: ArtifactRef(s.image.artifact_id, s.image.sha256)
                for s in base.manifest.scenes
            },
            videos={sid: p.ref for sid, p in base.videos.items()},
            voices={sid: p.ref for sid, p in voices.items()},
        )
    )
    return base, manifest, Pinned(artifact=s2, ref=s2_ref), voices


def test_render_inputs_accept_the_rebound_script_only_when_passed_in() -> None:
    from domain.errors import RenderInputIntegrityError
    from domain.render.plan import check_render_inputs

    base, manifest, script, voices = _rebound_render_inputs()
    kwargs: dict[str, Any] = dict(
        manifest=manifest,
        script=script,
        storyboard=base.storyboard,
        voices=voices,
        videos=base.videos,
    )
    # 結び直しを知らない呼び出し（既定）: storyboard の台本と違うので従来どおり拒否
    with pytest.raises(RenderInputIntegrityError, match="storyboard.source_script"):
        check_render_inputs(**kwargs)
    check_render_inputs(**kwargs, bound_script=script.ref)
    with pytest.raises(RenderInputIntegrityError, match="storyboard.source_script"):
        check_render_inputs(**kwargs, bound_script=base.script.ref)
    # 従来の一式は既定でも、元の台本を明示しても通る
    legacy: dict[str, Any] = dict(
        manifest=base.manifest,
        script=base.script,
        storyboard=base.storyboard,
        voices=base.voices,
        videos=base.videos,
    )
    check_render_inputs(**legacy)
    check_render_inputs(**legacy, bound_script=base.script.ref)


# --------------------------------------------------------------------------- render（Activity）


async def _edit_and_reassemble(session_factory, store, seed, *, link: bool) -> None:
    """台本を文だけ直し（必要なら結び直し）、音声とマニフェストを新しい台本から作り直す。"""
    from tests.support.render_activity import record_manifest, record_scene_voice

    edited = await _record_script(session_factory, store, seed.episode_id, _edited())
    if link:
        await _rebind(session_factory, seed.episode_id, edited.id, seed.storyboard.id)
    seed.script = edited
    for script_scene_id, scene_ids in {"s1": ["sb1"], "s2": ["sb2", "sb3"], "s3": ["sb4"]}.items():
        await record_scene_voice(session_factory, store, seed, script_scene_id, scene_ids)
    await record_manifest(session_factory, store, seed)


async def test_render_uses_the_rebound_script_and_keeps_the_scene_videos(
    session_factory, artifact_store, tmp_path
) -> None:
    from contracts.artifacts import parse_final_video
    from tests.support.render_activity import seed_render_inputs
    from tests.unit.test_render_activities import Harness, _req

    harness = Harness(session_factory, artifact_store, tmp_path)
    seed = await seed_render_inputs(session_factory, artifact_store)
    first = await harness.activities.render_final_video(_req(seed))
    videos_before = {k: m.id for k, m in seed.videos.items()}

    await _edit_and_reassemble(session_factory, artifact_store, seed, link=True)
    second = await harness.activities.render_final_video(_req(seed, run="run-2"))

    assert not second.skipped and second.artifact_id != first.artifact_id
    async with session_factory() as session:
        row = await ArtifactMetadataRepository(session).get(second.artifact_id)
    assert row is not None
    final = parse_final_video(await artifact_store.get_json(row.object_key))
    assert final.source_script.artifact_id == seed.script.id
    assert final.source_storyboard.artifact_id == seed.storyboard.id
    assert {k: m.id for k, m in seed.videos.items()} == videos_before  # 動画は作り直していない


async def test_render_without_a_link_still_refuses_a_script_the_storyboard_was_not_made_from(
    session_factory, artifact_store, tmp_path
) -> None:
    from temporalio.exceptions import ApplicationError

    from tests.support.render_activity import seed_render_inputs
    from tests.unit.test_render_activities import Harness, _req

    harness = Harness(session_factory, artifact_store, tmp_path)
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _edit_and_reassemble(session_factory, artifact_store, seed, link=False)
    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))
    assert info.value.type == "RenderInputIntegrityError"
    assert "storyboard.source_script" in str(info.value)


def test_structure_check_rejects_a_visual_edit() -> None:
    """映像の指示（``visual``）の変更は結び直しで済まない。

    storyboard・画像は旧い visual から作った。

    映像を変えるのは ``scene.override_visual`` か storyboard の作り直し（クロスレビュー HIGH）。
    """
    original = _script([dict(s) for s in SCRIPT_SCENES])
    with pytest.raises(ProductionInputInvalidError, match="visual"):
        check_rebound_script(original, _script(_edited(visual="炉の跡")))


async def test_production_plan_refuses_a_rebound_script_with_another_visual(
    session_factory,
) -> None:
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    _, storyboard_meta = await record_inputs(session_factory, store, episode)
    changed = await _record_script(session_factory, store, episode, _edited(visual="炉の跡"))
    await _rebind(session_factory, episode, changed.id, storyboard_meta.id)
    activities = ProductionActivities(session_factory=session_factory, store=store, bucket=BUCKET)
    with pytest.raises(ProductionInputInvalidError, match="visual"):
        await activities.plan(
            ProductionPlanRequest(episode_id=episode, workflow_id="w", run_id="r")
        )
