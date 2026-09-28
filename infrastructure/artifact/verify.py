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
from domain.production.identity import recipe_family
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
    tolerate_recipe_version: bool = False,
) -> ArtifactVerificationResult:
    """DB行が指す実体を検証し、verdictを返す。副作用（削除・変更）は一切起こさない。

    ``tolerate_recipe_version``（ADR-0035 (4)）: 生成設定版の比較で prompt 組み立て規則の版
    （末尾の ``prompt-v<N>``）の違いだけを許す。生成器・モデルの違いは許さない。

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
        artifact.artifact_type,
        parsed,
        current_generation_profile_id,
        tolerate_recipe_version=tolerate_recipe_version,
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
    artifact_type: ArtifactType,
    parsed: object,
    current_generation_profile_id: str | None,
    *,
    tolerate_recipe_version: bool = False,
) -> tuple[bool, bool]:
    """(profile_check_applicable, profile_id_valid) を返す。型に概念が無ければ (False, True)。"""
    if artifact_type in _GENERATOR_PROFILE_CHECKED_TYPES:
        if current_generation_profile_id is None:
            return False, True  # 呼び出し側が現在値を知らない・使わない（未評価。推測しない）
        generator = getattr(parsed, "generator", None)
        profile_id = getattr(generator, "generation_profile_id", None)
        if tolerate_recipe_version and isinstance(profile_id, str):
            return True, recipe_family(profile_id) == recipe_family(current_generation_profile_id)
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
    content_fingerprint: str | None = None,
    legacy_input_hashes: tuple[str, ...] = (),
) -> ArtifactMetadata | None:
    """再利用の唯一のゲート（ADR-0033 §3、ADR-0035 (4)）。

    次の順に同じシーンの**現行世代**を探し、見つかった行を実体検証する。verdict が
    ``REUSABLE`` でなければ「現行が無い」のと同じ ``None`` を返す（呼び出し側は既存の新ラウンド
    経路にそのまま進む。regenerate と retry を1つの述語に集約する ── ADR-0033 §3）。

    1. ``input_hash`` 完全一致（生成設定版も完全一致で検証）
    2. ``content_fingerprint`` 一致（レシピの版だけが違う成功済み成果物。生成器・モデルの違いは
       許さない）。prompt 組み立て規則の版を上げても成功済みのシーンを作り直さない（INV-33）
    3. ``legacy_input_hashes`` のどれかと一致する旧方式の行（``content_fingerprint`` が NULL）。
       呼び出し側が旧方式（4d96027 まで）で再計算した hash を渡す

    ``repo`` は ``infrastructure.db.repositories.ArtifactMetadataRepository`` 互換。型を
    狭めないのは、このモジュールが repository の型を import すると循環 import になりうるため。
    """
    current = await repo.find_current(  # type: ignore[attr-defined]
        episode_id, artifact_type, input_hash, scene_id
    )
    if current is not None:
        result = await verify_artifact(
            store, current, current_generation_profile_id=current_generation_profile_id
        )
        return current if result.verdict is ArtifactVerdict.REUSABLE else None

    candidate = None
    if content_fingerprint is not None:
        candidate = await repo.find_current_by_content_fingerprint(  # type: ignore[attr-defined]
            episode_id, artifact_type, content_fingerprint, scene_id
        )
    if candidate is None and legacy_input_hashes:
        candidate = await repo.find_current_legacy(  # type: ignore[attr-defined]
            episode_id, artifact_type, legacy_input_hashes, scene_id
        )
    if candidate is None:
        return None
    result = await verify_artifact(
        store,
        candidate,
        current_generation_profile_id=current_generation_profile_id,
        tolerate_recipe_version=True,
    )
    if result.verdict is not ArtifactVerdict.REUSABLE:
        return None
    return candidate
