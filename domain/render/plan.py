"""描画計画の組み立て（ADR-0019 §2 / §3）。純粋関数のみ。

入力は Activity が読み戻して契約検証した Artifact と、その参照（id / sha256）。
ここでは相互の整合性（マニフェストの参照 = 渡された Artifact、全て同じ storyboard / 台本から
作られたこと、網羅）を検査してから時間軸・音声・字幕を組む。

失敗の写像:
- 素材がマニフェストにあるのに渡されていない → ``RenderInputMissingError``
- 参照の id / sha256 の食い違い・余計な素材・契約違反 → ``RenderInputIntegrityError``
- 尺を合わせられない・総尺が profile の最短最長を外れる → ``DurationReconciliationError``
- 音声が重なる → ``VoiceTimelineOverflowError``
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from contracts.artifacts import (
    ProductionManifest,
    SceneVideoArtifact,
    SceneVoiceArtifact,
    ScriptArtifact,
    StoryboardArtifact,
)
from contracts.render import RenderEngineIdentity, RenderPlan, RenderProfile, TimelinePolicy
from domain.errors import (
    DurationReconciliationError,
    ProductionInputInvalidError,
    RenderInputIntegrityError,
    RenderInputMissingError,
)
from domain.production.manifest import ArtifactRef, check_manifest_coverage, check_member_sources
from domain.render.identity import render_plan_sha256
from domain.render.subtitles import build_subtitle_cues
from domain.render.timeline import SceneVideoSource, VoiceSource, build_timeline


@dataclass(frozen=True, slots=True)
class Pinned[A]:
    """読み戻して検証した Artifact 本体と、その保存時の参照。"""

    artifact: A
    ref: ArtifactRef


def _check_ref(label: str, ref: ArtifactRef, artifact_id: str, sha256: str) -> None:
    if (ref.artifact_id, ref.sha256) != (artifact_id, sha256):
        raise RenderInputIntegrityError(
            f"{label}: manifest pins {artifact_id}/{sha256}, got {ref.artifact_id}/{ref.sha256}"
        )


def _check_keys(label: str, provided: Mapping[str, object], expected: list[str]) -> None:
    missing = [key for key in expected if key not in provided]
    if missing:
        raise RenderInputMissingError(f"missing {label} for {missing}")
    extra = sorted(set(provided) - set(expected))
    if extra:
        raise RenderInputIntegrityError(f"unexpected {label} for {extra}")


def check_render_inputs(
    *,
    manifest: ProductionManifest,
    script: Pinned[ScriptArtifact],
    storyboard: Pinned[StoryboardArtifact],
    voices: Mapping[str, Pinned[SceneVoiceArtifact]],
    videos: Mapping[str, Pinned[SceneVideoArtifact]],
) -> None:
    """マニフェストと渡された入力の整合性。"""
    episode_id = manifest.episode_id
    _check_ref(
        "storyboard",
        storyboard.ref,
        manifest.source_storyboard.artifact_id,
        manifest.source_storyboard.sha256,
    )
    _check_ref(
        "script", script.ref, manifest.source_script.artifact_id, manifest.source_script.sha256
    )
    source_script = storyboard.artifact.source_script
    _check_ref(
        "storyboard.source_script", script.ref, source_script.artifact_id, source_script.sha256
    )
    for label, found in (
        ("script", script.artifact.episode_id),
        ("storyboard", storyboard.artifact.episode_id),
    ):
        if found != episode_id:
            raise RenderInputIntegrityError(f"{label} belongs to episode {found}, not {episode_id}")
    try:
        check_manifest_coverage(
            manifest,
            storyboard.artifact,
            script.artifact,
            storyboard_sha256=storyboard.ref.sha256,
            script_sha256=script.ref.sha256,
        )
    except ProductionInputInvalidError as exc:
        raise RenderInputIntegrityError(str(exc)) from exc

    _check_keys("scene video", videos, [s.scene_id for s in manifest.scenes])
    _check_keys("voice", voices, [v.script_scene_id for v in manifest.voices])
    for scene in manifest.scenes:
        pinned = videos[scene.scene_id]
        _check_ref(
            f"video {scene.scene_id}", pinned.ref, scene.video.artifact_id, scene.video.sha256
        )
        if pinned.artifact.scene_id != scene.scene_id:
            raise RenderInputIntegrityError(
                f"video for {scene.scene_id} is for scene {pinned.artifact.scene_id}"
            )
    for voice in manifest.voices:
        pinned_voice = voices[voice.script_scene_id]
        _check_ref(
            f"voice {voice.script_scene_id}", pinned_voice.ref, voice.artifact_id, voice.sha256
        )
        if pinned_voice.artifact.script_scene_id != voice.script_scene_id:
            raise RenderInputIntegrityError(
                f"voice for {voice.script_scene_id} is for {pinned_voice.artifact.script_scene_id}"
            )
    members = [*(p.artifact for p in videos.values()), *(p.artifact for p in voices.values())]
    for member in members:
        if member.episode_id != episode_id:
            raise RenderInputIntegrityError(
                f"{type(member).__name__} belongs to episode {member.episode_id}"
            )
    try:
        check_member_sources(
            members, storyboard_sha256=storyboard.ref.sha256, script_sha256=script.ref.sha256
        )
    except ProductionInputInvalidError as exc:
        raise RenderInputIntegrityError(str(exc)) from exc


def build_render_plan(
    *,
    manifest: ProductionManifest,
    script: Pinned[ScriptArtifact],
    storyboard: Pinned[StoryboardArtifact],
    voices: Mapping[str, Pinned[SceneVoiceArtifact]],
    videos: Mapping[str, Pinned[SceneVideoArtifact]],
    profile: RenderProfile,
    policy: TimelinePolicy,
    engine: RenderEngineIdentity,
) -> RenderPlan:
    """固定した入力 + profile + policy + engine から描画計画を決定的に組む。"""
    check_render_inputs(
        manifest=manifest, script=script, storyboard=storyboard, voices=voices, videos=videos
    )
    layout = build_timeline(
        storyboard.artifact,
        script.artifact,
        {
            sid: SceneVideoSource(
                scene_id=sid,
                artifact_id=p.ref.artifact_id,
                sha256=p.ref.sha256,
                duration_ms=p.artifact.duration_ms,
            )
            for sid, p in videos.items()
        },
        {
            sid: VoiceSource(
                script_scene_id=sid,
                artifact_id=p.ref.artifact_id,
                sha256=p.ref.sha256,
                duration_ms=p.artifact.duration_ms,
                storyboard_scene_ids=tuple(p.artifact.storyboard_scene_ids),
            )
            for sid, p in voices.items()
        },
        policy,
    )
    limits = profile.limits
    if not limits.min_duration_ms <= layout.total_duration_ms <= limits.max_duration_ms:
        # 描画前に弾く（描画後の技術検査でも同じ規則を見る）。素材か profile を人間が選び直す
        raise DurationReconciliationError(
            f"timeline total {layout.total_duration_ms} ms is outside profile "
            f"{profile.profile_id} limits [{limits.min_duration_ms}, {limits.max_duration_ms}] ms"
        )
    cues = build_subtitle_cues(script.artifact, layout.voices, profile.subtitles)
    try:
        return RenderPlan(
            scenes=layout.scenes,
            voices=layout.voices,
            subtitle_cues=cues,
            total_duration_ms=layout.total_duration_ms,
            profile=profile,
            policy=policy,
            engine=engine,
        )
    except ValidationError as exc:
        raise RenderInputIntegrityError(str(exc)[:1000]) from exc


__all__ = ["Pinned", "build_render_plan", "check_render_inputs", "render_plan_sha256"]
