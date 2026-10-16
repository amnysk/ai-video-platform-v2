"""描画の時間軸（ADR-0019 §3）。純粋関数のみ。

- 時間軸 = storyboard のシーン順と storyboard の尺（開始は累積、隙間・重なりなし）
- シーン動画の実尺 A と目標 T: A = T → exact / A > T → trim（先頭 T ms）/
  A < T かつ T − A ≤ max_freeze_ms → freeze_tail / それ以外 → ``DurationReconciliationError``
- **速度変更・ループはしない**
- 音声は台本シーンが参照する最初の storyboard シーンの開始に置く。重なりは
  ``VoiceTimelineOverflowError``。最後の音声だけ、総尺を ``max_freeze_ms`` 以内で超えてよく、
  その分だけ最終シーンの尺を延ばす（最終シーンの freeze は合計で ``max_freeze_ms`` まで）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from contracts.artifact_refs import ArtifactDigestRef
from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from contracts.render import (
    RenderTimelineScene,
    RenderVoicePlacement,
    SceneReconciliation,
    TimelinePolicy,
)
from domain.errors import (
    DurationReconciliationError,
    RenderInputIntegrityError,
    RenderInputMissingError,
    VoiceTimelineOverflowError,
)


@dataclass(frozen=True, slots=True)
class SceneVideoSource:
    """時間軸に載せるシーン動画（Artifact の参照と実尺だけ）。"""

    scene_id: str
    artifact_id: str
    sha256: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class VoiceSource:
    """時間軸に載せるナレーション音声（Artifact の参照・実尺・対応する storyboard シーン）。"""

    script_scene_id: str
    artifact_id: str
    sha256: str
    duration_ms: int
    storyboard_scene_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TimelineLayout:
    scenes: tuple[RenderTimelineScene, ...]
    voices: tuple[RenderVoicePlacement, ...]
    total_duration_ms: int


def reconcile_scene_duration(
    *, scene_id: str, requested_ms: int, source_ms: int, max_freeze_ms: int
) -> SceneReconciliation:
    """実尺と目標尺の合わせ方を1つ決める。合わせられなければ ``DurationReconciliationError``。"""
    if source_ms == requested_ms:
        return SceneReconciliation(mode="exact", trim_ms=0, freeze_ms=0)
    if source_ms > requested_ms:
        return SceneReconciliation(mode="trim", trim_ms=source_ms - requested_ms, freeze_ms=0)
    shortfall = requested_ms - source_ms
    if shortfall <= max_freeze_ms:
        return SceneReconciliation(mode="freeze_tail", trim_ms=0, freeze_ms=shortfall)
    raise DurationReconciliationError(
        f"scene {scene_id}: video is {source_ms} ms but storyboard requires {requested_ms} ms; "
        f"shortfall {shortfall} ms exceeds max_freeze_ms {max_freeze_ms}"
    )


def build_scene_timeline(
    storyboard: StoryboardArtifact,
    videos: Mapping[str, SceneVideoSource],
    policy: TimelinePolicy,
) -> tuple[RenderTimelineScene, ...]:
    """storyboard の順と尺でシーンを並べる。"""
    scenes: list[RenderTimelineScene] = []
    cursor = 0
    for scene in storyboard.scenes:
        video = videos.get(scene.scene_id)
        if video is None:
            raise RenderInputMissingError(f"no scene video for {scene.scene_id}")
        reconciliation = reconcile_scene_duration(
            scene_id=scene.scene_id,
            requested_ms=scene.duration_ms,
            source_ms=video.duration_ms,
            max_freeze_ms=policy.max_freeze_ms,
        )
        scenes.append(
            RenderTimelineScene(
                scene_id=scene.scene_id,
                order=scene.order,
                source_video=ArtifactDigestRef(artifact_id=video.artifact_id, sha256=video.sha256),
                timeline_start_ms=cursor,
                timeline_duration_ms=scene.duration_ms,
                requested_duration_ms=scene.duration_ms,
                source_duration_ms=video.duration_ms,
                reconciliation=reconciliation,
            )
        )
        cursor += scene.duration_ms
    return tuple(scenes)


def _expected_storyboard_scene_ids(
    storyboard: StoryboardArtifact, script_scene_id: str
) -> list[str]:
    indices = [i for i, s in enumerate(storyboard.scenes) if s.script_scene_id == script_scene_id]
    if not indices:
        raise RenderInputIntegrityError(f"script scene {script_scene_id} has no storyboard scene")
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise RenderInputIntegrityError(
            f"storyboard scenes for {script_scene_id} are not contiguous"
        )
    return [storyboard.scenes[i].scene_id for i in indices]


def _extend_last_scene(
    scene: RenderTimelineScene, extra_ms: int, policy: TimelinePolicy
) -> RenderTimelineScene:
    """最終シーンを ``extra_ms`` だけ延ばす。trim があれば先に戻し、残りを freeze にする。"""
    new_duration = scene.timeline_duration_ms + extra_ms
    source = scene.source_duration_ms
    if new_duration == source:
        reconciliation = SceneReconciliation(mode="exact", trim_ms=0, freeze_ms=0)
    elif new_duration < source:
        reconciliation = SceneReconciliation(
            mode="trim", trim_ms=source - new_duration, freeze_ms=0
        )
    else:
        freeze = new_duration - source
        if freeze > policy.max_freeze_ms:
            raise VoiceTimelineOverflowError(
                f"last scene {scene.scene_id} would need {freeze} ms freeze "
                f"(> max_freeze_ms {policy.max_freeze_ms}) to fit the last voice"
            )
        reconciliation = SceneReconciliation(mode="freeze_tail", trim_ms=0, freeze_ms=freeze)
    return scene.model_copy(
        update={"timeline_duration_ms": new_duration, "reconciliation": reconciliation}
    )


def place_voices(
    storyboard: StoryboardArtifact,
    script: ScriptArtifact,
    scenes: tuple[RenderTimelineScene, ...],
    voices: Mapping[str, VoiceSource],
    policy: TimelinePolicy,
) -> TimelineLayout:
    """台本の順に音声を置き、必要なら最終シーンを延ばした時間軸を返す。"""
    starts = {scene.scene_id: scene.timeline_start_ms for scene in scenes}
    total = sum(scene.timeline_duration_ms for scene in scenes)
    placements: list[RenderVoicePlacement] = []
    previous: RenderVoicePlacement | None = None
    for script_scene in script.scenes:
        sid = script_scene.id
        voice = voices.get(sid)
        if voice is None:
            raise RenderInputMissingError(f"no voice for script scene {sid}")
        expected = _expected_storyboard_scene_ids(storyboard, sid)
        if list(voice.storyboard_scene_ids) != expected:
            raise RenderInputIntegrityError(
                f"voice {sid} storyboard_scene_ids {list(voice.storyboard_scene_ids)} "
                f"!= storyboard {expected}"
            )
        placement = RenderVoicePlacement(
            script_scene_id=sid,
            source_voice=ArtifactDigestRef(artifact_id=voice.artifact_id, sha256=voice.sha256),
            start_ms=starts[expected[0]],
            duration_ms=voice.duration_ms,
            storyboard_scene_ids=tuple(expected),
        )
        if previous is not None and previous.end_ms > placement.start_ms:
            raise VoiceTimelineOverflowError(
                f"voice {previous.script_scene_id} ends at {previous.end_ms} ms, after voice "
                f"{sid} starts at {placement.start_ms} ms"
            )
        placements.append(placement)
        previous = placement
    extra = set(voices) - {p.script_scene_id for p in placements}
    if extra:
        raise RenderInputIntegrityError(f"voices for unknown script scenes {sorted(extra)}")

    last_end = placements[-1].end_ms
    out_scenes = scenes
    if last_end > total:
        overflow = last_end - total
        out_scenes = (*scenes[:-1], _extend_last_scene(scenes[-1], overflow, policy))
        total = last_end
    return TimelineLayout(scenes=out_scenes, voices=tuple(placements), total_duration_ms=total)


def build_timeline(
    storyboard: StoryboardArtifact,
    script: ScriptArtifact,
    videos: Mapping[str, SceneVideoSource],
    voices: Mapping[str, VoiceSource],
    policy: TimelinePolicy,
) -> TimelineLayout:
    """シーンの並びと音声の配置をまとめて組む。"""
    scenes = build_scene_timeline(storyboard, videos, policy)
    return place_voices(storyboard, script, scenes, voices, policy)


__all__ = [
    "SceneVideoSource",
    "TimelineLayout",
    "VoiceSource",
    "build_scene_timeline",
    "build_timeline",
    "place_voices",
    "reconcile_scene_duration",
]
