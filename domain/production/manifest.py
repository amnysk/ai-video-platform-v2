"""ProductionManifest の組み立てとカバレッジ検査（ADR-0017）。純粋関数のみ。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

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
    manifest: ProductionManifest,
    storyboard: StoryboardArtifact,
    script: ScriptArtifact,
    *,
    storyboard_sha256: str,
    script_sha256: str,
) -> None:
    """保存済みマニフェストが**この** storyboard / 台本を過不足なく覆うこと。

    シーン ID の並びだけでは、同じ構成で中身の違う storyboard（再計画）を見分けられないので、
    入力 Artifact の sha256 もマニフェストの固定値と照合する。
    """
    if manifest.source_storyboard.sha256 != storyboard_sha256:
        raise ProductionInputInvalidError(
            f"manifest source_storyboard sha256 {manifest.source_storyboard.sha256} "
            f"!= current storyboard {storyboard_sha256}"
        )
    if manifest.source_script.sha256 != script_sha256:
        raise ProductionInputInvalidError(
            f"manifest source_script sha256 {manifest.source_script.sha256} "
            f"!= current script {script_sha256}"
        )
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


class _SourceRef(Protocol):
    @property
    def sha256(self) -> str: ...


class _StoryboardMember(Protocol):
    @property
    def source_storyboard(self) -> _SourceRef: ...


def check_member_sources(
    members: Iterable[_StoryboardMember],
    *,
    storyboard_sha256: str,
    script_sha256: str | None = None,
) -> None:
    """組み立て時に、マニフェストへ載せる素材が全て同じ storyboard（と台本）から作られたこと。

    ``build_manifest`` は参照（id / sha256）しか受け取らないので、素材 Artifact 本体を読んだ
    呼び出し側がこれを通す。``source_script`` を持つ素材（音声）は ``script_sha256`` とも照合する。
    """
    for member in members:
        found = member.source_storyboard.sha256
        if found != storyboard_sha256:
            raise ProductionInputInvalidError(
                f"{type(member).__name__} was produced from storyboard {found}, "
                f"expected {storyboard_sha256}"
            )
        source_script = getattr(member, "source_script", None)
        if (
            script_sha256 is not None
            and source_script is not None
            and source_script.sha256 != script_sha256
        ):
            raise ProductionInputInvalidError(
                f"{type(member).__name__} was produced from script "
                f"{source_script.sha256}, expected {script_sha256}"
            )
