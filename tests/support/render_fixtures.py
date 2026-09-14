"""Render テストの共通部品（ADR-0019）。

互いに整合した入力一式（台本・storyboard・シーン動画・音声・マニフェスト）を作る。
sha256 は各 Artifact の正準 JSON から計算するので、他の agent の Activity テストでも
「保存して読み戻した」ものとしてそのまま使える。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from contracts.artifacts import (
    ProductionManifest,
    SceneVideoArtifact,
    SceneVoiceArtifact,
    ScriptArtifact,
    StoryboardArtifact,
    build_scene_video_artifact,
    build_scene_voice_artifact,
    parse_production_manifest,
    parse_scene_video_artifact,
    parse_scene_voice_artifact,
)
from contracts.render import (
    RenderEngineIdentity,
    RenderPlan,
    RenderProfile,
    TimelinePolicy,
    get_render_profile,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.production.manifest import ArtifactRef, build_manifest
from domain.render.plan import Pinned, build_render_plan
from domain.render.ports import FinalVideoInfo
from tests.support.production import sample_script, sample_storyboard

EPISODE_ID = "ep-render-1"
ENGINE = RenderEngineIdentity(engine="fake-engine", version="1.0.0", binary_sha256="e" * 64)
FONT_SHA256 = "f" * 64

#: sample_storyboard の尺（sb1=s1, sb2/sb3=s2, sb4=s3）
STORYBOARD_DURATIONS: dict[str, int] = {"sb1": 8000, "sb2": 4000, "sb3": 5000, "sb4": 8000}
#: 既定の音声尺（各台本シーンの窓に収まる）
VOICE_DURATIONS: dict[str, int] = {"s1": 7000, "s2": 8500, "s3": 7500}
VOICE_SCENES: dict[str, tuple[str, ...]] = {"s1": ("sb1",), "s2": ("sb2", "sb3"), "s3": ("sb4",)}


def _sha(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def _ref(model: Any) -> ArtifactRef:
    return ArtifactRef(artifact_id=str(uuid.uuid4()), sha256=_sha(model.model_dump(mode="json")))


@dataclass(frozen=True)
class RenderInputs:
    manifest: ProductionManifest
    manifest_ref: ArtifactRef
    script: Pinned[ScriptArtifact]
    storyboard: Pinned[StoryboardArtifact]
    voices: dict[str, Pinned[SceneVoiceArtifact]]
    videos: dict[str, Pinned[SceneVideoArtifact]]

    def plan(
        self,
        *,
        profile: RenderProfile | None = None,
        policy: TimelinePolicy | None = None,
        engine: RenderEngineIdentity = ENGINE,
    ) -> RenderPlan:
        return build_render_plan(
            manifest=self.manifest,
            script=self.script,
            storyboard=self.storyboard,
            voices=self.voices,
            videos=self.videos,
            profile=profile or get_render_profile("shorts_vertical"),
            policy=policy or TimelinePolicy(),
            engine=engine,
        )


def render_inputs(
    *,
    episode_id: str = EPISODE_ID,
    script: ScriptArtifact | None = None,
    video_durations: Mapping[str, int] | None = None,
    voice_durations: Mapping[str, int] | None = None,
) -> RenderInputs:
    """整合した入力一式。尺は ``video_durations`` / ``voice_durations`` で上書きできる。

    ``script`` を渡すときは sample_storyboard と同じ s1..s3 構成であること。
    """
    script_model = script if script is not None else sample_script(episode_id)
    script_ref = _ref(script_model)
    storyboard_model = sample_storyboard(
        episode_id, script_artifact_id=script_ref.artifact_id, script_sha256=script_ref.sha256
    )
    storyboard_ref = _ref(storyboard_model)
    sb_src = {
        "artifact_id": storyboard_ref.artifact_id,
        "sha256": storyboard_ref.sha256,
        "schema_version": "1.0",
    }
    sc_src = {
        "artifact_id": script_ref.artifact_id,
        "sha256": script_ref.sha256,
        "schema_version": "1.0",
    }
    generator = {"generator": "fake", "generator_model": "fake", "generation_profile_id": "p1"}

    videos: dict[str, Pinned[SceneVideoArtifact]] = {}
    for sid, requested in STORYBOARD_DURATIONS.items():
        duration = (video_durations or {}).get(sid, requested)
        video = parse_scene_video_artifact(
            build_scene_video_artifact(
                episode_id=episode_id,
                source_storyboard=sb_src,
                scene_id=sid,
                source_image={"artifact_id": str(uuid.uuid4()), "sha256": "1" * 64},
                media={
                    "object_key": f"media/{episode_id}/scene_video/{sid}/{'2' * 64}.mp4",
                    "sha256": "2" * 64,
                    "bytes": 1000,
                    "mime": "video/mp4",
                },
                duration_ms=duration,
                requested_duration_ms=requested,
                width=1080,
                height=1920,
                fps_millis=24_000,
                generator=generator,
            )
        )
        videos[sid] = Pinned(artifact=video, ref=_ref(video))

    voices: dict[str, Pinned[SceneVoiceArtifact]] = {}
    for sid, default in VOICE_DURATIONS.items():
        voice = parse_scene_voice_artifact(
            build_scene_voice_artifact(
                episode_id=episode_id,
                source_storyboard=sb_src,
                source_script=sc_src,
                script_scene_id=sid,
                storyboard_scene_ids=list(VOICE_SCENES[sid]),
                language=script_model.language,
                voice_id="voice-1",
                media={
                    "object_key": f"media/{episode_id}/scene_voice/{sid}/{'3' * 64}.wav",
                    "sha256": "3" * 64,
                    "bytes": 1000,
                    "mime": "audio/wav",
                },
                duration_ms=(voice_durations or {}).get(sid, default),
                sample_rate_hz=22_050,
                channels=1,
                generator=generator,
            )
        )
        voices[sid] = Pinned(artifact=voice, ref=_ref(voice))

    manifest = parse_production_manifest(
        build_manifest(
            episode_id=episode_id,
            storyboard_ref=storyboard_ref,
            script_ref=script_ref,
            storyboard=storyboard_model,
            script=script_model,
            images={sid: ArtifactRef(str(uuid.uuid4()), "4" * 64) for sid in STORYBOARD_DURATIONS},
            videos={sid: p.ref for sid, p in videos.items()},
            voices={sid: p.ref for sid, p in voices.items()},
        )
    )
    return RenderInputs(
        manifest=manifest,
        manifest_ref=_ref(manifest),
        script=Pinned(artifact=script_model, ref=script_ref),
        storyboard=Pinned(artifact=storyboard_model, ref=storyboard_ref),
        voices=voices,
        videos=videos,
    )


def good_final_video_info(plan: RenderPlan, **overrides: Any) -> FinalVideoInfo:
    """計画と profile にぴったり合う測定値。``overrides`` で1項目ずつ壊せる。"""
    profile = plan.profile
    info = FinalVideoInfo(
        duration_ms=plan.total_duration_ms,
        width=profile.width,
        height=profile.height,
        fps_millis=profile.fps_millis,
        frames_decoded=plan.total_duration_ms * profile.fps_millis // 1_000_000,
        decode_errors=0,
        video_codec=profile.video.codec,
        pix_fmt=profile.video.pix_fmt,
        audio_present=True,
        audio_codec=profile.audio.codec,
        audio_sample_rate_hz=profile.audio.sample_rate_hz,
        audio_channels=profile.audio.channels,
        audio_duration_ms=plan.total_duration_ms,
        bytes=123_456,
    )
    return replace(info, **overrides)


__all__ = [
    "ENGINE",
    "EPISODE_ID",
    "FONT_SHA256",
    "RenderInputs",
    "good_final_video_info",
    "render_inputs",
]
