"""Artifact再利用のI/O検証オーケストレーター（ADR-0033）。

``domain/artifact/verification.py`` の純粋関数へ渡す「事実」を実体（MinIO）から集める。
新しいストリーミング実装は作らない（``ArtifactStore.exists`` / ``stat`` / ``sha256_of`` を使う。
``sha256_of`` は既に流し読みでメモリに全体を載せない、``STREAM_CHUNK_BYTES=8MiB``）。

これが再利用の**唯一のゲート**になる（``find_and_verify_current``）。
通常パイプラインと ADR-0032 の統一再開 dry-run/実行は、どちらもこの同じ関数を呼ぶ。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pydantic

from contracts.artifacts import parse_artifact
from contracts.render import RENDER_PROFILES
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.verification import (
    ArtifactVerdict,
    ArtifactVerificationFacts,
    decide_verdict,
)
from infrastructure.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: image/video で生成設定版チェックの対象になる型。voice（Piper）はパラメータから都度導出される
#: ため対象外。render は別途 RENDER_PROFILES を見る（provider 固定値ではない）。
_GENERATOR_PROFILE_CHECKED_TYPES = frozenset({ArtifactType.SCENE_IMAGE, ArtifactType.SCENE_VIDEO})

__all__ = [
    "ArtifactVerificationResult",
    "find_and_verify_current",
    "verify_artifact",
]


@dataclass(frozen=True, slots=True)
class ArtifactVerificationResult:
    verdict: ArtifactVerdict
    #: 人が読める一文（ログ・診断用）。secretは含まない。
    detail: str


async def verify_artifact(
    store: ArtifactStore,
    artifact: ArtifactMetadata,
    *,
    current_generation_profile_id: str | None = None,
) -> ArtifactVerificationResult:
    """DB行が指す実体を検証し、verdictを返す。副作用（削除・変更）は一切起こさない。

    ``current_generation_profile_id``: image/video の生成設定版チェックに使う「現在有効な値」。
    fal の固定定数をここへ埋め込まない ── **今まさに構成されている generator が報告する値**
    （``domain.production.ports.ImageGenerator/VideoGenerator.generation_profile_id``）を
    呼び出し側（``PaidJobRunner.submit``）が渡す。fake/real どちらの generator でも同じ形で効き、
    provider・モデルを差し替えても本モジュールの変更が要らない（実装時の訂正、ADR-0033 §2-3）。
    省略時（``None``）はこの型のチェックを行わない（推測しない。安全側ではなく「未評価」を選ぶ
    ── render の `FINAL_VIDEO` など、この引数を使わない呼び出し元のための既定）。
    """
    try:
        descriptor_stat = await store.stat(artifact.object_key)
        descriptor_payload = await store.get_json(artifact.object_key)
    except KeyError:
        return _result(ArtifactVerdict.MISSING, f"descriptor object missing: {artifact.object_key}")

    try:
        parsed = parse_artifact(descriptor_payload)
    except (ValueError, pydantic.ValidationError) as exc:
        return _result(ArtifactVerdict.CORRUPT_SCHEMA, f"schema validation failed: {exc}")

    media = getattr(parsed, "media", None)
    media_required = media is not None
    media_exists = True
    media_content_verified = True
    if media_required:
        try:
            media_stat = await store.stat(media.object_key)
            media_sha256 = await store.sha256_of(media.object_key)
        except KeyError:
            media_exists = False
        else:
            media_content_verified = media_sha256 == media.sha256 and media_stat.size == media.bytes

    descriptor_sha256 = await store.sha256_of(artifact.object_key)
    descriptor_content_verified = descriptor_sha256 == artifact.sha256 and (
        artifact.size_bytes is None or descriptor_stat.size == artifact.size_bytes
    )

    profile_check_applicable, profile_id_valid = _check_profile(
        artifact.artifact_type, parsed, current_generation_profile_id
    )

    facts = ArtifactVerificationFacts(
        descriptor_exists=True,
        schema_valid=True,
        media_required=media_required,
        media_exists=media_exists,
        descriptor_content_verified=descriptor_content_verified,
        media_content_verified=media_content_verified,
        profile_check_applicable=profile_check_applicable,
        profile_id_valid=profile_id_valid,
    )
    verdict = decide_verdict(facts)
    if verdict is ArtifactVerdict.REUSABLE:
        return _result(verdict, "verified")

    detail = (
        f"artifact_id={artifact.id} artifact_type={artifact.artifact_type.value} "
        f"episode_id={artifact.episode_id} verdict={verdict.value}"
    )
    logger.error("ARTIFACT_VERIFICATION_FAILED %s", detail)
    return _result(verdict, detail)


def _result(verdict: ArtifactVerdict, detail: str) -> ArtifactVerificationResult:
    return ArtifactVerificationResult(verdict=verdict, detail=detail)


def _check_profile(
    artifact_type: ArtifactType, parsed: object, current_generation_profile_id: str | None
) -> tuple[bool, bool]:
    """(profile_check_applicable, profile_id_valid) を返す。型に概念が無ければ (False, True)。"""
    if artifact_type in _GENERATOR_PROFILE_CHECKED_TYPES:
        if current_generation_profile_id is None:
            return False, True  # 呼び出し側が現在値を知らない・使わない（未評価。推測しない）
        generator = getattr(parsed, "generator", None)
        profile_id = getattr(generator, "generation_profile_id", None)
        return True, profile_id == current_generation_profile_id
    if artifact_type is ArtifactType.FINAL_VIDEO:
        render_profile = getattr(parsed, "render_profile", None)
        profile_id = getattr(render_profile, "profile_id", None)
        return True, profile_id in RENDER_PROFILES
    return False, True


async def find_and_verify_current(
    *,
    repo: object,
    store: ArtifactStore,
    episode_id: str,
    artifact_type: ArtifactType,
    input_hash: str,
    scene_id: str | None = None,
    current_generation_profile_id: str | None = None,
) -> ArtifactMetadata | None:
    """再利用の唯一のゲート（ADR-0033 §3）。

    既存の ``ArtifactMetadataRepository.find_current`` を呼び、行が見つかれば検証する。
    verdict が ``REUSABLE`` でなければ「現行が無い」のと同じ ``None`` を返す
    （呼び出し側は既存の新ラウンド経路にそのまま進む。regenerate と retry を1つの述語に
    集約する ── ADR-0033 §3）。

    ``repo`` は ``infrastructure.db.repositories.ArtifactMetadataRepository`` 互換
    （``find_current`` を持つ）。型を狭めないのは、このモジュールが repository の型を
    import すると循環 import になりうるため（repositories.py がこちらを呼ぶ構成にする）。

    ``current_generation_profile_id`` は ``verify_artifact`` へそのまま渡す
    （image/video の生成設定版チェック。渡さない呼び出し元ではその型のチェックを行わない）。
    """
    current = await repo.find_current(  # type: ignore[attr-defined]
        episode_id, artifact_type, input_hash, scene_id
    )
    if current is None:
        return None
    result = await verify_artifact(
        store, current, current_generation_profile_id=current_generation_profile_id
    )
    if result.verdict is not ArtifactVerdict.REUSABLE:
        return None
    return current
