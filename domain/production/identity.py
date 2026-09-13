"""シーン素材の入力指紋（ADR-0012 / ADR-0017）。純粋関数のみ。

``input_hash`` の**構成要素の定義はここに1つだけ**置く（AGENTS.md §8）。
冪等キーは台本と同じ規則なので ``domain.script.identity.idempotency_key`` を再利用する。

どの関数も含めない: ラウンド番号 / 試行回数 / job_id / 時刻 / workflow run id / seed。
provider・モデル・パラメータは ``generator_id`` と ``generation_profile_id`` が覆う。
"""

from __future__ import annotations

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.script.identity import idempotency_key

__all__ = [
    "idempotency_key",
    "image_input_hash",
    "narration_sha256",
    "video_input_hash",
    "voice_input_hash",
]


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
    narration_sha256: str,
    voice_id: str,
    language: str,
    speed_permille: int,
    generator_id: str,
    generation_profile_id: str,
) -> str:
    """ナレーション音声の入力指紋。速度は float を避けて permille の int。"""
    return _digest(
        {
            "episode_id": episode_id,
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "script_sha256": script_sha256,
            "script_scene_id": script_scene_id,
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
