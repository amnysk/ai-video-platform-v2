"""INV-33（ADR-0035 (4)）: シーン単位の再利用。SQLite + メモリストア + fake 生成器。

1シーンの映像の差し替え（代替映像案）と prompt 組み立て規則の版の変更は、そのシーンと
依存成果物以外を再生成・再課金しない。旧方式（4d96027 まで）の成果物・予約とも照合する。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from temporalio.exceptions import ApplicationError

import domain.production.prompting as prompting
from contracts.artifacts import (
    build_scene_visual_override_artifact,
    parse_storyboard_artifact,
)
from contracts.states import ArtifactType, ProviderCall, ReservationStatus
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.artifact.verification import ArtifactVerdict
from domain.production.identity import (
    idempotency_key,
    image_input_hash,
    recipe_version_candidates,
    video_input_hash,
)
from infrastructure.artifact.verify import verify_artifact
from infrastructure.db.models import ArtifactMetadataRow, ProviderReservationRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.production import FakeImageGenerator, FakeVideoGenerator
from tests.unit.test_production_image_activities import _await as image_await
from tests.unit.test_production_image_activities import _submit as image_submit
from tests.unit.test_production_image_activities import make_activities as make_image
from tests.unit.test_production_image_activities import seed_storyboard
from tests.unit.test_production_video_activities import _await as video_await
from tests.unit.test_production_video_activities import _submit as video_submit
from tests.unit.test_production_video_activities import make_activities as make_video
from tests.unit.test_production_video_activities import seed as seed_video


async def _produce_image(acts, episode_id, sb_id, scene):
    submitted = await acts.submit(image_submit(episode_id, sb_id, scene=scene))
    assert submitted.artifact is None, f"{scene} was expected to be newly generated"
    return await acts.await_image(image_await(episode_id, sb_id, submitted.reservation_id, scene))


async def _storyboard(session_factory, store, sb_id):
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).get(sb_id)
    assert meta is not None
    return meta, parse_storyboard_artifact(await store.get_json(meta.object_key))


async def _record_override(session_factory, store, episode_id, sb_meta, scene_id, description):
    payload = build_scene_visual_override_artifact(
        episode_id=episode_id,
        source_storyboard={
            "artifact_id": sb_meta.id,
            "sha256": sb_meta.sha256,
            "schema_version": "1.0",
        },
        scene_id=scene_id,
        revision=1,
        visual_kind="broll",
        visual_subject="landscape",
        visual_description=description,
        framing="wide establishing shot",
        camera_movement=None,
        rationale="The site conveys the event; names stay in narration.",
        rejection_ids=[str(uuid.uuid4())],
        planner={
            "generator": "fake-planner",
            "generator_model": "fake",
            "generation_profile_id": "scene-alternative-v1",
        },
    )
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(
        episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE.value, digest, scene_id
    )
    await store.put_json(key, payload)
    async with session_factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.SCENE_VISUAL_OVERRIDE,
            schema_version="1.0",
            bucket="artifacts",
            object_key=key,
            sha256=digest,
            scene_id=scene_id,
        )
        await session.commit()


async def _make_legacy(session_factory, artifact_id: str, legacy_hash: str) -> None:
    """本番に残る 4d96027 までの行を模す: 旧方式の input_hash、content_fingerprint なし。

    成果物を作った予約も同じ旧方式の hash を持つ（本番の実際の形）。
    """
    async with session_factory() as session:
        await session.execute(
            update(ArtifactMetadataRow)
            .where(ArtifactMetadataRow.id == uuid.UUID(artifact_id))
            .values(input_hash=legacy_hash, content_fingerprint=None)
        )
        rows = (
            await session.scalars(
                select(ProviderReservationRow).where(
                    ProviderReservationRow.outcome_artifact_id == uuid.UUID(artifact_id)
                )
            )
        ).all()
        for row in rows:  # 冪等キーも hash から導かれる（旧方式の予約は旧方式のキーを持つ）
            row.input_hash = legacy_hash
            row.idempotency_key = idempotency_key(
                provider=row.provider, input_hash=legacy_hash, round=row.round
            )
        await session.commit()


def _legacy_image_hash(episode_id, sb_meta, scene, version_index=0) -> str:
    style = recipe_version_candidates(prompting.DEFAULT_IMAGE_STYLE.style_profile_id)[version_index]
    return image_input_hash(
        episode_id=episode_id,
        artifact_type="scene_image",
        schema_version="1.0",
        storyboard_sha256=sb_meta.sha256,
        scene_id=scene.scene_id,
        visual_description=scene.visual_description,
        visual_kind=scene.visual_kind.value,
        framing=scene.framing,
        style_profile_id=style,
        generator_id="fake-image",
        generation_profile_id="fake-image-profile-v1",
    )


def _bump(monkeypatch, name: str) -> None:
    monkeypatch.setattr(prompting, name, str(int(getattr(prompting, name)) + 1))


# ======================================================================== 画像


async def test_image_recipe_version_bump_reuses_every_succeeded_scene(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    """ADR-0034 の版 1→2 の上げ方が、成功済みシーンを全部再課金した事故の再発防止。"""
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_image(session_factory, artifact_store, gen, tmp_path)
    for scene in ("sb1", "sb2"):
        await _produce_image(acts, episode_id, sb_id, scene)
    assert gen.submit_calls == 2

    _bump(monkeypatch, "IMAGE_PROMPT_BUILDER_VERSION")
    for scene in ("sb1", "sb2"):
        again = await acts.submit(image_submit(episode_id, sb_id, scene=scene))
        assert again.artifact is not None and again.artifact.reused, scene
    assert gen.submit_calls == 2

    # まだ作っていないシーンは新しい版で作る
    await _produce_image(acts, episode_id, sb_id, "sb3")
    assert gen.submit_calls == 3


async def test_override_regenerates_only_the_overridden_scene_image(
    session_factory, artifact_store, tmp_path
) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_image(session_factory, artifact_store, gen, tmp_path)
    first = {s: await _produce_image(acts, episode_id, sb_id, s) for s in ("sb1", "sb2")}
    sb_meta, _ = await _storyboard(session_factory, artifact_store, sb_id)

    alternative = "The castle gate at dawn seen from across the moat"
    await _record_override(session_factory, artifact_store, episode_id, sb_meta, "sb2", alternative)

    sb1 = await acts.submit(image_submit(episode_id, sb_id, scene="sb1"))
    assert sb1.artifact is not None and sb1.artifact.artifact_id == first["sb1"].artifact_id
    # fake は同じバイト列を返すので Artifact 自体は内容で重複排除される（INV-17）。
    # 検査するのは「sb2 だけが provider へ再 submit された」こと
    await _produce_image(acts, episode_id, sb_id, "sb2")
    assert gen.submit_calls == 3
    # 実効シーン（代替案）でプロンプトを組み立てている
    prompts = [job.request.prompt for job in gen._jobs.values()]  # noqa: SLF001
    assert prompts[-1].startswith(alternative)


async def test_legacy_image_artifact_is_reused_without_a_new_submit(
    session_factory, artifact_store, tmp_path
) -> None:
    """本番の旧方式の行（content_fingerprint NULL）も、同じ内容なら作り直さない。"""
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_image(session_factory, artifact_store, gen, tmp_path)
    produced = await _produce_image(acts, episode_id, sb_id, "sb1")
    sb_meta, storyboard = await _storyboard(session_factory, artifact_store, sb_id)
    await _make_legacy(
        session_factory,
        produced.artifact_id,
        _legacy_image_hash(episode_id, sb_meta, storyboard.scenes[0]),
    )

    again = await acts.submit(image_submit(episode_id, sb_id, scene="sb1"))
    assert again.artifact is not None and again.artifact.artifact_id == produced.artifact_id
    assert gen.submit_calls == 1


async def test_unmatched_legacy_image_artifact_is_not_reused(
    session_factory, artifact_store, tmp_path
) -> None:
    """旧方式で再計算しても一致しない行（別の入力から作られた）は再利用しない。"""
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_image(session_factory, artifact_store, gen, tmp_path)
    produced = await _produce_image(acts, episode_id, sb_id, "sb1")
    await _make_legacy(session_factory, produced.artifact_id, "0" * 64)

    await _produce_image(acts, episode_id, sb_id, "sb1")
    assert gen.submit_calls == 2


async def test_legacy_image_of_an_overridden_scene_is_not_reused(
    session_factory, artifact_store, tmp_path
) -> None:
    """拒否されて代替案が立ったシーンでは、旧方式の元画像（拒否された画像）を使わない。"""
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_image(session_factory, artifact_store, gen, tmp_path)
    produced = await _produce_image(acts, episode_id, sb_id, "sb1")
    sb_meta, storyboard = await _storyboard(session_factory, artifact_store, sb_id)
    await _make_legacy(
        session_factory,
        produced.artifact_id,
        _legacy_image_hash(episode_id, sb_meta, storyboard.scenes[0]),
    )
    await _record_override(
        session_factory, artifact_store, episode_id, sb_meta, "sb1", "A map of the Tokaido road"
    )

    await _produce_image(acts, episode_id, sb_id, "sb1")
    assert gen.submit_calls == 2


# ======================================================================== 動画


async def _produce_video(acts, ep, sb, img, scene="sb1"):
    submitted = await acts.submit(video_submit(ep, sb, img, scene=scene))
    assert submitted.artifact is None
    return await acts.await_video(video_await(ep, sb, img, submitted.reservation_id, scene))


async def _legacy_video_hash(session_factory, store, ep, sb_id, img_id, gen) -> str:
    sb_meta, storyboard = await _storyboard(session_factory, store, sb_id)
    async with session_factory() as session:
        image_meta = await ArtifactMetadataRepository(session).get(img_id)
    assert image_meta is not None
    image = await store.get_json(image_meta.object_key)
    scene = storyboard.scenes[0]
    motion_v1 = recipe_version_candidates(prompting.DEFAULT_VIDEO_MOTION.motion_profile_id)[0]
    return video_input_hash(
        episode_id=ep,
        artifact_type="scene_video",
        schema_version="1.0",
        storyboard_sha256=sb_meta.sha256,
        scene_id=scene.scene_id,
        source_image_sha256=image["media"]["sha256"],
        visual_description=scene.visual_description,
        camera_movement=scene.camera_movement,
        transition_in=scene.transition_in,
        requested_duration_ms=gen.supported_duration_ms(scene.duration_ms),
        generator_id=gen.generator_id,
        generation_profile_id=f"{gen.generation_profile_id}+{motion_v1}",
    )


async def test_video_recipe_version_bump_reuses_the_succeeded_video(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    ep, sb, images = await seed_video(session_factory, artifact_store)
    gen = FakeVideoGenerator(pending_polls=0)
    acts = make_video(session_factory, artifact_store, gen, tmp_path)
    produced = await _produce_video(acts, ep, sb, images["sb1"])

    _bump(monkeypatch, "VIDEO_PROMPT_BUILDER_VERSION")
    again = await acts.submit(video_submit(ep, sb, images["sb1"]))
    assert again.artifact is not None and again.artifact.artifact_id == produced.artifact_id
    assert gen.submit_calls == 1


async def test_legacy_video_artifact_is_reused(session_factory, artifact_store, tmp_path) -> None:
    ep, sb, images = await seed_video(session_factory, artifact_store)
    gen = FakeVideoGenerator(pending_polls=0)
    acts = make_video(session_factory, artifact_store, gen, tmp_path)
    produced = await _produce_video(acts, ep, sb, images["sb1"])
    legacy = await _legacy_video_hash(session_factory, artifact_store, ep, sb, images["sb1"], gen)
    await _make_legacy(session_factory, produced.artifact_id, legacy)

    again = await acts.submit(video_submit(ep, sb, images["sb1"]))
    assert again.artifact is not None and again.artifact.artifact_id == produced.artifact_id
    assert gen.submit_calls == 1


async def test_in_flight_legacy_reservation_is_resumed_not_resubmitted(
    session_factory, artifact_store, tmp_path
) -> None:
    """hash の方式が変わっても、旧方式で submit 済みの課金ジョブへ二重 submit しない。"""
    ep, sb, images = await seed_video(session_factory, artifact_store)
    gen = FakeVideoGenerator(pending_polls=0)
    acts = make_video(session_factory, artifact_store, gen, tmp_path)
    submitted = await acts.submit(video_submit(ep, sb, images["sb1"]))
    legacy = await _legacy_video_hash(session_factory, artifact_store, ep, sb, images["sb1"], gen)
    async with session_factory() as session:
        await session.execute(
            update(ProviderReservationRow)
            .where(ProviderReservationRow.id == uuid.UUID(submitted.reservation_id))
            .values(input_hash=legacy)
        )
        await session.commit()

    again = await acts.submit(video_submit(ep, sb, images["sb1"]))
    assert again.reservation_id == submitted.reservation_id
    assert gen.submit_calls == 1
    result = await acts.await_video(video_await(ep, sb, images["sb1"], again.reservation_id))
    assert result.reused is False
    assert gen.submit_calls == 1


async def test_legacy_rejected_input_is_not_resubmitted_under_the_new_hash(
    session_factory, artifact_store, tmp_path
) -> None:
    """本番の 422 行（旧方式 hash・input_rejected_by_provider）を新方式で素通りしない（INV-32）。"""
    ep, sb, images = await seed_video(session_factory, artifact_store)
    gen = FakeVideoGenerator(pending_polls=0)
    acts = make_video(session_factory, artifact_store, gen, tmp_path)
    legacy = await _legacy_video_hash(session_factory, artifact_store, ep, sb, images["sb1"], gen)
    async with session_factory() as session:
        session.add(
            ProviderReservationRow(
                id=uuid.uuid4(),
                episode_id=uuid.UUID(ep),
                provider=ProviderCall.FAL_VIDEO.value,
                idempotency_key=sha256_hex(f"legacy-{legacy}".encode()),
                input_hash=legacy,
                round=1,
                status=ReservationStatus.SPENT.value,
                failure_class="needs_input",
                error_summary="ProviderRejectedError: fal job failed: HTTP 422",
                input_rejected_by_provider=True,
                scene_id="sb1",
                provider_job_ref="legacy-job",
                reserved_at=datetime.now(UTC),
                reconciled_at=datetime.now(UTC),
            )
        )
        await session.commit()

    with pytest.raises(ApplicationError) as info:
        await acts.submit(video_submit(ep, sb, images["sb1"]))
    assert info.value.type == "ProviderRejectedRetryBlockedError"
    assert gen.submit_calls == 0
    async with session_factory() as session:
        latest = await ProviderReservationRepository(session).find_unreconciled(
            episode_id=ep, provider=ProviderCall.FAL_VIDEO, scene_id="sb1"
        )
    assert latest == []


# ======================================================================== 検証（verify）


def _video_payload(profile_id: str, body: bytes) -> dict:
    return {
        "episode_id": "ep",
        "type": "scene_video",
        "schema_version": "1.0",
        "source_storyboard": {
            "artifact_id": str(uuid.uuid4()),
            "sha256": "a" * 64,
            "schema_version": "1.0",
        },
        "scene_id": "sb1",
        "source_image": {"artifact_id": str(uuid.uuid4()), "sha256": "b" * 64},
        "media": {
            "object_key": "m.mp4",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "mime": "video/mp4",
        },
        "duration_ms": 1000,
        "requested_duration_ms": 1000,
        "width": 720,
        "height": 1280,
        "fps_millis": 24000,
        "has_audio": False,
        "generator": {
            "generator": "fal",
            "generator_model": "seedance",
            "generation_profile_id": profile_id,
        },
    }


@pytest.mark.parametrize(
    ("stored", "tolerant_verdict"),
    [
        ("gen-v1+motion-v1:video-prompt-v1", ArtifactVerdict.REUSABLE),
        ("gen-v0+motion-v1:video-prompt-v1", ArtifactVerdict.VERSION_MISMATCH),
    ],
)
async def test_recipe_tolerance_forgives_only_the_prompt_builder_version(
    stored, tolerant_verdict
) -> None:
    store = InMemoryArtifactStore()
    body = b"mp4"
    put = await store.put_json("d.json", _video_payload(stored, body))
    await store.put_bytes("m.mp4", body, "video/mp4")
    meta = ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id="ep",
        artifact_type=ArtifactType.SCENE_VIDEO,
        schema_version="1.0",
        bucket="b",
        object_key="d.json",
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        scene_id="sb1",
        size_bytes=put.size,
    )
    current = "gen-v1+motion-v1:video-prompt-v2"
    strict = await verify_artifact(store, meta, current_generation_profile_id=current)
    tolerant = await verify_artifact(
        store, meta, current_generation_profile_id=current, tolerate_recipe_version=True
    )
    assert strict.verdict is ArtifactVerdict.VERSION_MISMATCH
    assert tolerant.verdict is tolerant_verdict
