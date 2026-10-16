"""実効シーンとシーン単位の指紋（ADR-0035）。純粋関数のみ。

「実効シーン」は storyboard のシーンに、そのシーンの現行の代替映像案（``SceneVisualOverride``）を
重ねたもの。画像・動画はこれを入力にする。storyboard 全体の世代は変えないので、差し替えた
シーン以外の指紋は変わらない（INV-33）。
"""

from __future__ import annotations

from contracts.artifacts import SceneVisualOverrideArtifact, StoryboardScene
from domain.artifact.hashing import canonical_json_bytes, sha256_hex

__all__ = [
    "SceneOverrideMismatchError",
    "apply_override",
    "scene_visual_fingerprint",
]

#: 指紋に入れない項目: タイムライン上の位置（前のシーンの尺が変わるとずれるだけで、
#: このシーンが何を映すかは変わらない）。
_POSITION_FIELDS = frozenset({"order", "start_ms"})


class SceneOverrideMismatchError(ValueError):
    """別シーンの代替案を重ねようとした。"""


def apply_override(
    scene: StoryboardScene, override: SceneVisualOverrideArtifact | None
) -> StoryboardScene:
    """映像の項目だけを差し替える。時間割と台本との対応は差し替えない。"""
    if override is None:
        return scene
    if override.scene_id != scene.scene_id:
        raise SceneOverrideMismatchError(
            f"override for {override.scene_id} cannot be applied to {scene.scene_id}"
        )
    return scene.model_copy(
        update={
            "visual_kind": override.visual_kind,
            "visual_subject": override.visual_subject,
            "visual_description": override.visual_description,
            "framing": override.framing,
            "camera_movement": override.camera_movement,
        }
    )


def scene_visual_fingerprint(scene: StoryboardScene) -> str:
    """このシーンが何を映すかの指紋。``None`` の任意項目は材料に含めない（旧形式と一致）。"""
    payload = scene.model_dump(mode="json", exclude=set(_POSITION_FIELDS), exclude_none=True)
    return sha256_hex(canonical_json_bytes(payload))
