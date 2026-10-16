"""シーン素材の入力指紋（ADR-0012 / ADR-0017）。純粋関数のみ。

``input_hash`` の**構成要素の定義はここに1つだけ**置く（AGENTS.md §8）。
冪等キーは台本と同じ規則なので ``domain.script.identity.idempotency_key`` を再利用する。

どの関数も含めない: ラウンド番号 / 試行回数 / job_id / 時刻 / workflow run id / seed。
provider・モデル・パラメータは ``generator_id`` と ``generation_profile_id`` が覆う。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.script.identity import idempotency_key

__all__ = [
    "IDENTITY_SCHEME_VERSION",
    "idempotency_key",
    "image_content_fingerprint",
    "image_input_hash",
    "image_input_hash_v2",
    "narration_sha256",
    "recipe_family",
    "recipe_version_candidates",
    "video_content_fingerprint",
    "video_generation_profile_id",
    "video_input_hash",
    "video_input_hash_v2",
    "voice_input_hash",
]

#: 画像・動画の input_hash の方式（ADR-0035 (4)）。方式1（``image_input_hash`` /
#: ``video_input_hash``）は 4d96027 以前に作られた本番 Artifact・予約を照合するために残す。
IDENTITY_SCHEME_VERSION = 2

#: prompt 組み立て規則の版（``domain.production.prompting`` の ``...prompt-v<N>``）。
_RECIPE_VERSION_RE = re.compile(r"(prompt-v)(\d+)$")


def _digest(payload: dict[str, object]) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def narration_sha256(text: str) -> str:
    """ナレーション文の指紋。本文を input_hash の材料へ直接入れない（台本が単一の真実）。"""
    return sha256_hex(text.encode("utf-8"))


def image_input_hash(
    *,
    episode_id: str,
    artifact_type: str,
    schema_version: str,
    storyboard_sha256: str,
    scene_id: str,
    visual_description: str,
    visual_kind: str,
    framing: str | None,
    style_profile_id: str,
    generator_id: str,
    generation_profile_id: str,
) -> str:
    """静止画の入力指紋。動きの指示（camera_movement / transition_in）は含めない。"""
    return _digest(
        {
            "episode_id": episode_id,
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "storyboard_sha256": storyboard_sha256,
            "scene_id": scene_id,
            "visual_description": visual_description,
            "visual_kind": visual_kind,
            "framing": framing,
            "style_profile_id": style_profile_id,
            "generator_id": generator_id,
            "generation_profile_id": generation_profile_id,
        }
    )


def voice_input_hash(
    *,
    episode_id: str,
    artifact_type: str,
    schema_version: str,
    script_sha256: str,
    script_scene_id: str,
    storyboard_sha256: str,
    storyboard_scene_ids: Iterable[str],
    narration_sha256: str,
    voice_id: str,
    language: str,
    speed_permille: int,
    generator_id: str,
    generation_profile_id: str,
) -> str:
    """ナレーション音声の入力指紋。速度は float を避けて permille の int。

    音声 Artifact は ``source_storyboard`` と ``storyboard_scene_ids`` を記録するので、
    storyboard の再計画で古い音声を再利用しないよう両方を含める（参照集合は整列して正規化）。
    """
    return _digest(
        {
            "episode_id": episode_id,
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "script_sha256": script_sha256,
            "script_scene_id": script_scene_id,
            "storyboard_sha256": storyboard_sha256,
            "storyboard_scene_ids": sorted(storyboard_scene_ids),
            "narration_sha256": narration_sha256,
            "voice_id": voice_id,
            "language": language,
            "speed_permille": speed_permille,
            "generator_id": generator_id,
            "generation_profile_id": generation_profile_id,
        }
    )


def video_input_hash(
    *,
    episode_id: str,
    artifact_type: str,
    schema_version: str,
    storyboard_sha256: str,
    scene_id: str,
    source_image_sha256: str,
    visual_description: str,
    camera_movement: str | None,
    transition_in: str | None,
    requested_duration_ms: int,
    generator_id: str,
    generation_profile_id: str,
) -> str:
    """動画の入力指紋。元画像の sha256 を含むので、画像が変われば動画も再生成対象になる。"""
    return _digest(
        {
            "episode_id": episode_id,
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "storyboard_sha256": storyboard_sha256,
            "scene_id": scene_id,
            "source_image_sha256": source_image_sha256,
            "visual_description": visual_description,
            "camera_movement": camera_movement,
            "transition_in": transition_in,
            "requested_duration_ms": requested_duration_ms,
            "generator_id": generator_id,
            "generation_profile_id": generation_profile_id,
        }
    )


# --------------------------------------------------------------------------- 方式2（ADR-0035）
#
# 方式1は storyboard 全体の sha256 とシーンの一部の項目を材料にしていた。方式2は
# 「そのシーンの実効内容の指紋」（``domain.production.effective_scene.scene_visual_fingerprint``）
# を材料にし、代替映像案（override）で差し替えたシーンの hash だけが変わるようにする。
# storyboard の sha256 は残す: 画像・動画 Artifact は ``source_storyboard`` を記録し、
# manifest・render はそれが Episode の storyboard と一致することを検査するため
# （別の storyboard 世代の成果物を再利用すると後工程で必ず止まる）。代替案は storyboard の
# 世代を変えないので、差し替えていないシーンの hash は変わらない。
#
# ``*_content_fingerprint`` は同じ材料から「レシピの版」（prompt 組み立て規則の版）だけを
# 除いた指紋。版だけが上がったときに成功済みの成果物を作り直さないための再利用キー。


def recipe_family(profile_id: str) -> str:
    """profile id から prompt 組み立て規則の版（末尾の ``prompt-v<N>``）だけを伏せる。

    生成器自身の版（``fake-image-profile-v1`` 等。モデル・パラメータ）は伏せない。
    """
    return _RECIPE_VERSION_RE.sub(r"\1*", profile_id)


def recipe_version_candidates(profile_id: str) -> list[str]:
    """同じ組み立て規則の、版 1 から現在の版までの profile id（旧方式の hash 再計算用）。"""
    match = _RECIPE_VERSION_RE.search(profile_id)
    if match is None:
        return [profile_id]
    prefix = profile_id[: match.start(2)]
    return [f"{prefix}{version}" for version in range(1, int(match.group(2)) + 1)]


def video_generation_profile_id(generator_profile_id: str, motion_profile_id: str) -> str:
    """動画の「生成設定版」: 生成器のプロファイルと動画プロンプト規則の版の合成。"""
    return f"{generator_profile_id}+{motion_profile_id}"


def _image_v2(
    *,
    episode_id: str,
    artifact_type: str,
    schema_version: str,
    storyboard_sha256: str,
    scene_id: str,
    scene_fingerprint: str,
    style_profile_id: str,
    generator_id: str,
    generation_profile_id: str,
) -> dict[str, object]:
    return {
        "identity_scheme": IDENTITY_SCHEME_VERSION,
        "episode_id": episode_id,
        "artifact_type": artifact_type,
        "schema_version": schema_version,
        "storyboard_sha256": storyboard_sha256,
        "scene_id": scene_id,
        "scene_fingerprint": scene_fingerprint,
        "style_profile_id": style_profile_id,
        "generator_id": generator_id,
        "generation_profile_id": generation_profile_id,
    }


def image_input_hash_v2(**fields: Any) -> str:
    """静止画の入力指紋（方式2）。``scene_fingerprint`` は動きの項目を除いた実効シーンの指紋。"""
    return _digest(_image_v2(**fields))


def image_content_fingerprint(**fields: Any) -> str:
    """方式2の静止画の材料から、スタイルの組み立て規則の版だけを除いた指紋。"""
    payload = _image_v2(**fields)
    payload["style_profile_id"] = recipe_family(str(payload["style_profile_id"]))
    return _digest({**payload, "fingerprint": "content"})


def _video_v2(
    *,
    episode_id: str,
    artifact_type: str,
    schema_version: str,
    storyboard_sha256: str,
    scene_id: str,
    scene_fingerprint: str,
    source_image_sha256: str,
    requested_duration_ms: int,
    generator_id: str,
    generator_profile_id: str,
    motion_profile_id: str,
) -> dict[str, object]:
    return {
        "identity_scheme": IDENTITY_SCHEME_VERSION,
        "episode_id": episode_id,
        "artifact_type": artifact_type,
        "schema_version": schema_version,
        "storyboard_sha256": storyboard_sha256,
        "scene_id": scene_id,
        "scene_fingerprint": scene_fingerprint,
        "source_image_sha256": source_image_sha256,
        "requested_duration_ms": requested_duration_ms,
        "generator_id": generator_id,
        "generator_profile_id": generator_profile_id,
        "motion_profile_id": motion_profile_id,
    }


def video_input_hash_v2(**fields: Any) -> str:
    """動画の入力指紋（方式2）。元画像の sha256 を含むので、画像が変われば動画も変わる。"""
    return _digest(_video_v2(**fields))


def video_content_fingerprint(**fields: Any) -> str:
    """方式2の動画の材料から、動画プロンプト規則の版だけを除いた指紋。"""
    payload = _video_v2(**fields)
    payload["motion_profile_id"] = recipe_family(str(payload["motion_profile_id"]))
    return _digest({**payload, "fingerprint": "content"})
