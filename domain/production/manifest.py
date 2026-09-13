"""ProductionManifest の組み立てとカバレッジ検査（ADR-0017）。純粋関数のみ。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracts.artifacts import (
    ProductionManifest,
    ScriptArtifact,
    StoryboardArtifact,
    build_production_manifest,
)
from domain.errors import ProductionInputInvalidError, ProductionInputMissingError


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    sha256: str
    schema_version: str = "1.0"


def build_manifest(
    *,
    episode_id: str,
    storyboard_ref: ArtifactRef,
    script_ref: ArtifactRef,
    storyboard: StoryboardArtifact,
    script: ScriptArtifact,
    images: Mapping[str, ArtifactRef],
    videos: Mapping[str, ArtifactRef],
    voices: Mapping[str, ArtifactRef],
) -> dict[str, Any]:
    """storyboard / 台本の順でマニフェストを組む。欠けは missing、余りは invalid。"""
    scene_ids = [s.scene_id for s in storyboard.scenes]
    script_ids = [s.id for s in script.scenes]
    for label, refs, expected in (
        ("image", images, scene_ids),
        ("video", videos, scene_ids),
        ("voice", voices, script_ids),
    ):
        missing = [key for key in expected if key not in refs]
        if missing:
            raise ProductionInputMissingError(f"missing {label} for scenes {missing}")
        extra = sorted(set(refs) - set(expected))
        if extra:
            raise ProductionInputInvalidError(f"unexpected {label} for scenes {extra}")

    def _src(ref: ArtifactRef) -> dict[str, str]:
        return {
            "artifact_id": ref.artifact_id,
            "sha256": ref.sha256,
            "schema_version": ref.schema_version,
        }

    def _digest(ref: ArtifactRef) -> dict[str, str]:
        return {"artifact_id": ref.artifact_id, "sha256": ref.sha256}

    try:
        return build_production_manifest(
            episode_id=episode_id,
            source_storyboard=_src(storyboard_ref),
            source_script=_src(script_ref),
            scenes=[
                {"scene_id": sid, "image": _digest(images[sid]), "video": _digest(videos[sid])}
                for sid in scene_ids
            ],
            voices=[{"script_scene_id": sid, **_digest(voices[sid])} for sid in script_ids],
        )
    except ValueError as exc:  # pydantic ValidationError を含む
        raise ProductionInputInvalidError(str(exc)[:1000]) from exc


def check_manifest_coverage(
    manifest: ProductionManifest, storyboard: StoryboardArtifact, script: ScriptArtifact
) -> None:
    """保存済みマニフェストが storyboard / 台本を過不足なく覆うこと。"""
    scene_ids = [s.scene_id for s in storyboard.scenes]
    if [s.scene_id for s in manifest.scenes] != scene_ids:
        raise ProductionInputInvalidError(
            f"manifest scenes {[s.scene_id for s in manifest.scenes]} != storyboard {scene_ids}"
        )
    script_ids = [s.id for s in script.scenes]
    if [v.script_scene_id for v in manifest.voices] != script_ids:
        raise ProductionInputInvalidError(
            f"manifest voices {[v.script_scene_id for v in manifest.voices]} != script {script_ids}"
        )
