"""テスト用に描画計画を手で組む（domain の計画組み立てに依存しない）。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from contracts.artifact_refs import ArtifactDigestRef
from contracts.render import (
    RENDER_PROFILES,
    RenderEngineIdentity,
    RenderPlan,
    RenderProfile,
    RenderTimelineScene,
    RenderVoicePlacement,
    SceneReconciliation,
    SubtitleCue,
    TimelinePolicy,
)

FAKE_ENGINE = RenderEngineIdentity(engine="fake", version="1", binary_sha256="a" * 64)


def _ref(name: str) -> ArtifactDigestRef:
    return ArtifactDigestRef(artifact_id=str(uuid.uuid5(uuid.NAMESPACE_URL, name)), sha256="b" * 64)


def reconciliation(source_ms: int, timeline_ms: int) -> SceneReconciliation:
    if source_ms == timeline_ms:
        return SceneReconciliation(mode="exact", trim_ms=0, freeze_ms=0)
    if source_ms > timeline_ms:
        return SceneReconciliation(mode="trim", trim_ms=source_ms - timeline_ms, freeze_ms=0)
    return SceneReconciliation(mode="freeze_tail", trim_ms=0, freeze_ms=timeline_ms - source_ms)


def make_plan(
    *,
    profile: RenderProfile | str = "shorts_vertical",
    scenes: Sequence[tuple[int, int]] = ((1000, 1000),),
    voices: Sequence[tuple[int, int]] | None = None,
    cues: Sequence[tuple[int, int, int]] = (),
    engine: RenderEngineIdentity = FAKE_ENGINE,
    subtitles: bool | None = None,
) -> RenderPlan:
    """scenes: (source_ms, timeline_ms)、voices: (start_ms, duration_ms)（台本シーン s1.. に対応、
    すべて全シーンではなく先頭シーンに紐づける）、cues: (voice_index, start_ms, end_ms)。"""
    prof = RENDER_PROFILES[profile] if isinstance(profile, str) else profile
    if subtitles is not None:
        prof = prof.model_copy(
            update={"subtitles": prof.subtitles.model_copy(update={"enabled": subtitles})}
        )
    timeline: list[RenderTimelineScene] = []
    start = 0
    for i, (source_ms, timeline_ms) in enumerate(scenes, start=1):
        timeline.append(
            RenderTimelineScene(
                scene_id=f"sb{i}",
                order=i,
                source_video=_ref(f"scene-{i}"),
                timeline_start_ms=start,
                timeline_duration_ms=timeline_ms,
                requested_duration_ms=timeline_ms,
                source_duration_ms=source_ms,
                reconciliation=reconciliation(source_ms, timeline_ms),
            )
        )
        start += timeline_ms
    voice_specs = voices if voices is not None else ((0, min(start, 1000)),)
    placements = tuple(
        RenderVoicePlacement(
            script_scene_id=f"s{k}",
            source_voice=_ref(f"voice-{k}"),
            start_ms=v_start,
            duration_ms=v_dur,
            storyboard_scene_ids=("sb1",),
        )
        for k, (v_start, v_dur) in enumerate(voice_specs, start=1)
    )
    subtitle_cues = tuple(
        SubtitleCue(
            cue_index=i,
            script_scene_id=f"s{voice_index + 1}",
            char_start=i * 10,
            char_end=i * 10 + 5,
            start_ms=c_start,
            end_ms=c_end,
        )
        for i, (voice_index, c_start, c_end) in enumerate(cues)
    )
    return RenderPlan(
        scenes=tuple(timeline),
        voices=placements,
        subtitle_cues=subtitle_cues,
        total_duration_ms=start,
        profile=prof,
        policy=TimelinePolicy(),
        engine=engine,
    )
