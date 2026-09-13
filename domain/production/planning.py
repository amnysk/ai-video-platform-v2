"""台本と storyboard から production の作業一覧を作る（ADR-0017）。純粋関数のみ。"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from domain.errors import ProductionInputInvalidError


@dataclass(frozen=True, slots=True)
class ImageWorkItem:
    scene_id: str


@dataclass(frozen=True, slots=True)
class VideoWorkItem:
    scene_id: str
    requested_duration_ms: int


@dataclass(frozen=True, slots=True)
class VoiceWorkItem:
    script_scene_id: str
    storyboard_scene_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProductionWorkList:
    images: tuple[ImageWorkItem, ...]
    videos: tuple[VideoWorkItem, ...]
    voices: tuple[VoiceWorkItem, ...]


def plan_production(script: ScriptArtifact, storyboard: StoryboardArtifact) -> ProductionWorkList:
    """storyboard シーンごとに画像+動画、台本シーンごとに音声。順序は各 Artifact の順。

    storyboard が参照しない台本シーン、台本に無いシーンを参照する storyboard は
    ``ProductionInputInvalidError``（入力同士の食い違いは人間が直す）。
    """
    if storyboard.episode_id != script.episode_id:
        raise ProductionInputInvalidError(
            f"storyboard episode {storyboard.episode_id} != script episode {script.episode_id}"
        )
    script_ids = [scene.id for scene in script.scenes]
    referenced: dict[str, list[str]] = {sid: [] for sid in script_ids}
    for scene in storyboard.scenes:
        if scene.script_scene_id not in referenced:
            raise ProductionInputInvalidError(
                f"storyboard scene {scene.scene_id} references unknown script scene "
                f"{scene.script_scene_id}"
            )
        referenced[scene.script_scene_id].append(scene.scene_id)
    uncovered = [sid for sid, scenes in referenced.items() if not scenes]
    if uncovered:
        raise ProductionInputInvalidError(f"script scenes not covered by storyboard: {uncovered}")

    return ProductionWorkList(
        images=tuple(ImageWorkItem(scene_id=s.scene_id) for s in storyboard.scenes),
        videos=tuple(
            VideoWorkItem(scene_id=s.scene_id, requested_duration_ms=s.duration_ms)
            for s in storyboard.scenes
        ),
        voices=tuple(
            VoiceWorkItem(script_scene_id=sid, storyboard_scene_ids=tuple(referenced[sid]))
            for sid in script_ids
        ),
    )
