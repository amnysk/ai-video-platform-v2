"""``infrastructure/diagnostics/episode_diagnostic.py`` の新規部分だけを検査する。

判定ロジック（``build_resume_plan`` / ``verify_artifact``）は既存のテストで検査済み
（``tests/unit/test_resume_plan.py`` / ``tests/unit/test_artifact_verify_io.py``）。ここで
検査するのは、このモジュールが**追加した**もの ── ``verify_episode_artifacts`` が既存の
``verify_artifact`` を Artifact ごとに正しく呼んで整形すること、``format_report`` が
判定を変えずに文字列へ整形するだけであること ── だけ。

``InMemoryArtifactStore``（既存の fake、``tests/unit/test_artifact_verify_io.py`` と同じ
ペイロードの組み立て方）を使う、隔離されたインメモリ fixture。実DB・実MinIOには触れない。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from contracts.pipeline import PipelineStage
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.verification import ArtifactVerdict
from domain.pipeline.resume_plan import ResumePlan
from infrastructure.diagnostics.episode_diagnostic import (
    EpisodeDiagnosticReport,
    ReservationRow,
    format_report,
    verify_episode_artifacts,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore

EPISODE_ID = str(uuid.uuid4())
CURRENT_IMAGE_PROFILE = "fake-image-profile-v1"
CURRENT_VIDEO_PROFILE = "fake-video-profile-v1"


def _source_ref() -> dict:
    return {"artifact_id": str(uuid.uuid4()), "sha256": "a" * 64, "schema_version": "1.0"}


def _image_payload(
    *, scene_id: str, generation_profile_id: str, media_key: str, media_body: bytes
) -> dict:
    return {
        "episode_id": EPISODE_ID,
        "type": "scene_image",
        "schema_version": "1.0",
        "source_storyboard": _source_ref(),
        "scene_id": scene_id,
        "media": {
            "object_key": media_key,
            "sha256": hashlib.sha256(media_body).hexdigest(),
            "bytes": len(media_body),
            "mime": "image/png",
        },
        "width": 1080,
        "height": 1920,
        "generator": {
            "generator": "fal",
            "generator_model": "seedream-4.5",
            "generation_profile_id": generation_profile_id,
        },
    }


async def _seed_image(
    store: InMemoryArtifactStore,
    *,
    scene_id: str,
    generation_profile_id: str = CURRENT_IMAGE_PROFILE,
    media_body: bytes = b"png-bytes",
    corrupt_after_seed: bool = False,
) -> ArtifactMetadata:
    descriptor_key = f"artifacts/{EPISODE_ID}/scene_image/{scene_id}.json"
    media_key = f"artifacts/{EPISODE_ID}/scene_image/{scene_id}.png"
    payload = _image_payload(
        scene_id=scene_id,
        generation_profile_id=generation_profile_id,
        media_key=media_key,
        media_body=media_body,
    )
    put = await store.put_json(descriptor_key, payload)
    await store.put_bytes(media_key, media_body, "image/png")
    if corrupt_after_seed:
        # 記録後に実体だけが壊れた状況を模す（MinIOの部分障害等、ADR-0033 §Context）。
        store._objects[media_key] = b"tampered"  # noqa: SLF001 -- 外部からの破損を模すだけ
    return ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="artifacts",
        object_key=descriptor_key,
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        scene_id=scene_id,
        size_bytes=put.size,
    )


def _profile_for(artifact_type: ArtifactType) -> str | None:
    if artifact_type is ArtifactType.SCENE_IMAGE:
        return CURRENT_IMAGE_PROFILE
    if artifact_type is ArtifactType.SCENE_VIDEO:
        return CURRENT_VIDEO_PROFILE
    return None


@pytest.fixture
def store() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


async def test_verify_episode_artifacts_marks_intact_artifact_reusable(
    store: InMemoryArtifactStore,
) -> None:
    sb1 = await _seed_image(store, scene_id="sb1")
    rows = await verify_episode_artifacts(store, [sb1], profile_for=_profile_for)
    assert len(rows) == 1
    assert rows[0].verdict is ArtifactVerdict.REUSABLE
    assert rows[0].scene_id == "sb1"
    assert rows[0].artifact_type is ArtifactType.SCENE_IMAGE


async def test_verify_episode_artifacts_flags_corrupted_media_without_hiding_it(
    store: InMemoryArtifactStore,
) -> None:
    """MinIO 実体が破損していれば MISSING/CORRUPT_* が誠実に出る（黙って REUSABLE にしない）。"""
    sb2 = await _seed_image(store, scene_id="sb2", corrupt_after_seed=True)
    rows = await verify_episode_artifacts(store, [sb2], profile_for=_profile_for)
    assert rows[0].verdict is ArtifactVerdict.CORRUPT_HASH


async def test_verify_episode_artifacts_flags_stale_generation_profile(
    store: InMemoryArtifactStore,
) -> None:
    """記録された生成設定版が「現在有効な値」と食い違えば VERSION_MISMATCH（ADR-0033 §2-3）。"""
    stale = await _seed_image(store, scene_id="sb3", generation_profile_id="retired-profile-v0")
    rows = await verify_episode_artifacts(store, [stale], profile_for=_profile_for)
    assert rows[0].verdict is ArtifactVerdict.VERSION_MISMATCH


async def test_verify_episode_artifacts_reports_missing_descriptor(
    store: InMemoryArtifactStore,
) -> None:
    ghost = ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="artifacts",
        object_key="artifacts/does/not/exist.json",
        sha256="0" * 64,
        created_at=datetime.now(UTC),
        scene_id="sb9",
        size_bytes=1,
    )
    rows = await verify_episode_artifacts(store, [ghost], profile_for=_profile_for)
    assert rows[0].verdict is ArtifactVerdict.MISSING


async def test_verify_episode_artifacts_does_not_write_to_the_store(
    store: InMemoryArtifactStore,
) -> None:
    """診断は読み取り専用（``InMemoryArtifactStore.write_count`` で検査する）。"""
    sb1 = await _seed_image(store, scene_id="sb1")
    descriptor_key = sb1.object_key
    media_key = f"artifacts/{EPISODE_ID}/scene_image/sb1.png"
    writes_before = (store.write_count(descriptor_key), store.write_count(media_key))
    await verify_episode_artifacts(store, [sb1], profile_for=_profile_for)
    writes_after = (store.write_count(descriptor_key), store.write_count(media_key))
    assert writes_before == writes_after


def _fake_report(*, verdict_by_scene: dict[str, ArtifactVerdict]) -> EpisodeDiagnosticReport:
    from infrastructure.diagnostics.episode_diagnostic import ArtifactVerificationRow

    verifications = tuple(
        ArtifactVerificationRow(
            artifact_type=ArtifactType.SCENE_VIDEO,
            scene_id=scene_id,
            artifact_id=str(uuid.uuid4()),
            object_key=f"artifacts/{EPISODE_ID}/scene_video/{scene_id}.json",
            verdict=verdict,
            detail="verified" if verdict is ArtifactVerdict.REUSABLE else "verdict != reusable",
        )
        for scene_id, verdict in verdict_by_scene.items()
    )
    reservation = ReservationRow(
        id=str(uuid.uuid4()),
        provider=ProviderCall.FAL_VIDEO,
        scene_id="sb1",
        round=1,
        status=ReservationStatus.SPENT.value,
        reconciled_by="evidence",
        has_raw_output=True,
        outcome_artifact_id=str(uuid.uuid4()),
        estimated_cost_usd="1.2095",
    )
    plan = ResumePlan(
        episode_id=EPISODE_ID,
        resumable=True,
        target_stage=PipelineStage.PRODUCTION.value,
        stages_to_run=(
            PipelineStage.PRODUCTION.value,
            PipelineStage.RENDER.value,
            PipelineStage.UPLOAD.value,
        ),
        unresolved_blockers=(),
        possible_new_charges=(PipelineStage.PRODUCTION.value, PipelineStage.UPLOAD.value),
        reason=None,
    )
    return EpisodeDiagnosticReport(
        episode_id=EPISODE_ID,
        status=EpisodeStatus.BLOCKED,
        blocked_reason="needs_input: fal storage token refused: HTTP 403",
        workflow_id=f"episode-{EPISODE_ID}-production:abc",
        resume_plan=plan,
        artifact_verifications=verifications,
        reservations=(reservation,),
        upload_receipt_present=False,
    )


def test_format_report_surfaces_every_verdict_without_altering_it() -> None:
    report = _fake_report(
        verdict_by_scene={
            "sb1": ArtifactVerdict.REUSABLE,
            "sb6": ArtifactVerdict.MISSING,
        }
    )
    text = format_report(report)
    assert "reusable" in text
    assert "missing" in text
    assert "sb1" in text
    assert "sb6" in text
    assert "upload_receipt_present: False" in text
    assert report.resume_plan.resumable is True  # 整形は判定を変えない


def test_format_report_does_not_silently_drop_a_non_reusable_verdict() -> None:
    """全滅ではなく一部だけ破損したケースでも、破損側が出力から消えないこと。"""
    report = _fake_report(
        verdict_by_scene={
            "sb6": ArtifactVerdict.VERSION_MISMATCH,
            "sb7": ArtifactVerdict.CORRUPT_HASH,
            "sb8": ArtifactVerdict.CORRUPT_SCHEMA,
            "sb9": ArtifactVerdict.MISSING,
        }
    )
    text = format_report(report)
    for verdict in ("version_mismatch", "corrupt_hash", "corrupt_schema", "missing"):
        assert verdict in text
    for scene in ("sb6", "sb7", "sb8", "sb9"):
        assert scene in text


def test_fake_report_helper_is_frozen_and_reusable() -> None:
    """テストの fixture 自体が壊れていないことの最小限のサニティ検査。"""
    a = _fake_report(verdict_by_scene={"sb1": ArtifactVerdict.REUSABLE})
    b = replace(a, upload_receipt_present=True)
    assert a.upload_receipt_present is False
    assert b.upload_receipt_present is True
