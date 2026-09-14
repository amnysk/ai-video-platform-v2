"""Render の契約（ADR-0019）: 出力 profile・時間軸ポリシー・描画計画・完成動画 Artifact。

- 出力の縦横比・解像度は **profile が決める**。9:16 を前提にしない（縦横比は幅と高さから導出する）
- 描画計画（``RenderPlan``）は固定した入力 + profile + policy の純粋関数の結果。別 Artifact として
  保存せず、正準 JSON の sha256 を ``FinalVideoArtifact.render_plan_sha256`` に残す
- ナレーション文は**複製しない**。字幕 cue は台本ナレーションへの文字オフセットだけを持つ
- 描画エンジンの名前（バイナリ名など）はここに書かない。``RenderEngineIdentity`` は値として運ぶだけ
- 時間はすべて int のミリ秒、fps は ``fps_millis``（float は正準 JSON の sha256 を揺らす）
"""

from __future__ import annotations

from math import gcd
from typing import Annotated, Literal

from pydantic import Field, model_validator

from contracts.artifact_refs import (
    SCRIPT_SCENE_ID_PATTERN,
    SHA256_HEX_PATTERN,
    STORYBOARD_SCENE_ID_PATTERN,
    ArtifactDigestRef,
    FrozenModel,
    SourceArtifactRef,
)
from contracts.states import ArtifactType

RENDER_ARTIFACT_SCHEMA_VERSION = "1.0"

#: 計画の組み立て・描画の意味（時間軸の規則・字幕の割り付け・音声の混合）を変えたら上げる。
#: input_hash に入るので、上げると同じ入力でも再描画される。
RENDER_TEMPLATE_VERSION = 1

# ----------------------------------------------------- 既定値の唯一の宣言元（ADR-0019 §12）

#: 既定の出力 profile。API / workflow 入力 / Activity がここを参照する。
DEFAULT_RENDER_PROFILE_ID = "shorts_vertical"
#: render worker の同時描画数（CPU・ディスクを占有するので 1）。
DEFAULT_RENDER_CONCURRENCY = 1
#: 描画 Activity の start_to_close。
DEFAULT_RENDER_TIMEOUT_SECONDS = 30 * 60
DEFAULT_RENDER_HEARTBEAT_TIMEOUT_SECONDS = 60
#: 作業領域に最低限残す空き容量（入力量から見積もった分に加算する）。
DEFAULT_RENDER_MIN_FREE_BYTES = 10 * 1024**3
#: シーン動画が storyboard の尺より短いとき、最後のフレームを保持してよい上限。
DEFAULT_RENDER_MAX_FREEZE_MS = 2_000
#: 描画エンジンに許すスレッド数。
DEFAULT_RENDER_FFMPEG_THREADS = 4

#: 完成動画1本の上限（バイト）。シーン素材の ``MEDIA_MAX_BYTES`` とは別（長尺を許す）。
FINAL_VIDEO_MAX_BYTES = 8 * 1024**3
FINAL_VIDEO_MIME_TYPE = "video/mp4"

RENDER_PROFILE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
QA_CHECK_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


# --------------------------------------------------------------------------- profile


class RenderVideoSettings(FrozenModel):
    codec: Literal["h264"]
    crf: int = Field(ge=0, le=51)
    preset: Literal["ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower"]
    pix_fmt: Literal["yuv420p"]
    #: キーフレーム間隔（フレーム数）。None はエンジン既定。
    gop_frames: int | None = Field(default=None, gt=0)


class RenderAudioSettings(FrozenModel):
    codec: Literal["aac"]
    bitrate_kbps: int = Field(ge=32, le=512)
    sample_rate_hz: Literal[44_100, 48_000]
    channels: Literal[1, 2]


class RenderSubtitleSettings(FrozenModel):
    enabled: bool
    font_size_px: int = Field(gt=0, le=400)
    margin_bottom_px: int = Field(ge=0)
    margin_side_px: int = Field(ge=0)
    max_chars_per_line: int = Field(gt=0, le=200)
    max_lines: int = Field(ge=1, le=4)
    alignment: Literal["bottom_center", "middle_center", "top_center"]


class RenderLimits(FrozenModel):
    min_duration_ms: int = Field(gt=0)
    max_duration_ms: int = Field(gt=0)
    #: 計画の総尺と実測の尺の許容差。
    duration_tolerance_ms: int = Field(ge=0, le=5_000)
    audio_required: bool

    @model_validator(mode="after")
    def _ordered(self) -> RenderLimits:
        if self.min_duration_ms > self.max_duration_ms:
            raise ValueError("min_duration_ms must be <= max_duration_ms")
        return self


class RenderProfile(FrozenModel):
    """出力の形（解像度・fps・収め方・符号化・字幕の配置・尺の制約）。

    production の生成 profile（``generation_profile_id``）とは別物。素材を作り直さずに
    出力だけを変えられる。
    """

    profile_id: str = Field(pattern=RENDER_PROFILE_ID_PATTERN)
    width: int = Field(gt=0, le=7680)
    height: int = Field(gt=0, le=7680)
    fps_millis: int = Field(ge=1_000, le=120_000)
    #: cover: 画面を埋めて溢れを切る / contain: 全体を収めて余白を黒で埋める
    fit: Literal["cover", "contain"]
    video: RenderVideoSettings
    audio: RenderAudioSettings
    subtitles: RenderSubtitleSettings
    limits: RenderLimits

    @property
    def aspect_ratio(self) -> str:
        """幅と高さの既約比（例 ``9:16``）。保存しない導出値。"""
        g = gcd(self.width, self.height)
        return f"{self.width // g}:{self.height // g}"

    @model_validator(mode="after")
    def _check(self) -> RenderProfile:
        if self.width % 2 or self.height % 2:
            raise ValueError("width and height must be even (yuv420p)")
        subs = self.subtitles
        if subs.margin_bottom_px >= self.height or 2 * subs.margin_side_px >= self.width:
            raise ValueError("subtitle margins must fit inside the frame")
        return self


#: 組み込み profile。id -> profile。追加は ADR-0019 の手順（契約テスト）に従う。
RENDER_PROFILES: dict[str, RenderProfile] = {
    profile.profile_id: profile
    for profile in (
        RenderProfile(
            profile_id="shorts_vertical",
            width=1080,
            height=1920,
            fps_millis=30_000,
            fit="cover",
            video=RenderVideoSettings(codec="h264", crf=20, preset="medium", pix_fmt="yuv420p"),
            audio=RenderAudioSettings(
                codec="aac", bitrate_kbps=192, sample_rate_hz=48_000, channels=2
            ),
            subtitles=RenderSubtitleSettings(
                enabled=True,
                font_size_px=56,
                margin_bottom_px=420,  # 高さの約22%（Shorts の UI に隠れない下部）
                margin_side_px=80,
                max_chars_per_line=16,
                max_lines=2,
                alignment="bottom_center",
            ),
            limits=RenderLimits(
                min_duration_ms=1_000,
                max_duration_ms=180_000,
                duration_tolerance_ms=100,
                audio_required=True,
            ),
        ),
        RenderProfile(
            profile_id="long_form_horizontal",
            width=1920,
            height=1080,
            fps_millis=30_000,
            fit="contain",  # Phase 4 の素材は 9:16。切らずに左右を黒で埋める
            video=RenderVideoSettings(codec="h264", crf=20, preset="medium", pix_fmt="yuv420p"),
            audio=RenderAudioSettings(
                codec="aac", bitrate_kbps=192, sample_rate_hz=48_000, channels=2
            ),
            subtitles=RenderSubtitleSettings(
                enabled=True,
                font_size_px=48,
                margin_bottom_px=86,  # 高さの約8%
                margin_side_px=160,
                max_chars_per_line=32,
                max_lines=2,
                alignment="bottom_center",
            ),
            limits=RenderLimits(
                min_duration_ms=1_000,
                max_duration_ms=3 * 60 * 60 * 1000,
                duration_tolerance_ms=100,
                audio_required=True,
            ),
        ),
    )
}


def get_render_profile(profile_id: str) -> RenderProfile:
    """登録済み profile を返す。未知の id は ``KeyError``。

    contracts は domain を import できない（INV-6）ので、``UnknownRenderProfileError`` への
    写像は呼び出し側（domain / worker / API）の責務。
    """
    try:
        return RENDER_PROFILES[profile_id]
    except KeyError:
        raise KeyError(f"unknown render profile: {profile_id!r}") from None


class TimelinePolicy(FrozenModel):
    """時間軸の規則（ADR-0019 §4）。input_hash に入る。"""

    max_freeze_ms: int = Field(default=DEFAULT_RENDER_MAX_FREEZE_MS, ge=0, le=10_000)
    #: 5A はカットのみ
    transition: Literal["cut"] = "cut"
    template_version: int = Field(default=RENDER_TEMPLATE_VERSION, ge=1)


class RenderEngineIdentity(FrozenModel):
    """描画エンジンの同一性。バイナリの sha256 まで固定する（input_hash に入る）。"""

    engine: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    binary_sha256: str = Field(pattern=SHA256_HEX_PATTERN)


# --------------------------------------------------------------------------- 計画


class SceneReconciliation(FrozenModel):
    """シーン動画の実尺と時間軸上の尺の合わせ方。速度変更・ループは語彙に無い。"""

    mode: Literal["exact", "trim", "freeze_tail"]
    trim_ms: int = Field(ge=0)
    freeze_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> SceneReconciliation:
        if self.mode == "exact" and (self.trim_ms or self.freeze_ms):
            raise ValueError("exact reconciliation has no trim or freeze")
        if self.mode == "trim" and (self.trim_ms <= 0 or self.freeze_ms):
            raise ValueError("trim reconciliation needs trim_ms > 0 and no freeze")
        if self.mode == "freeze_tail" and (self.freeze_ms <= 0 or self.trim_ms):
            raise ValueError("freeze_tail reconciliation needs freeze_ms > 0 and no trim")
        return self


class RenderTimelineScene(FrozenModel):
    """時間軸上の storyboard シーン1件。"""

    scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    order: int = Field(ge=1)
    source_video: ArtifactDigestRef
    timeline_start_ms: int = Field(ge=0)
    timeline_duration_ms: int = Field(gt=0)
    #: storyboard が求めた尺
    requested_duration_ms: int = Field(gt=0)
    #: シーン動画 Artifact の実尺
    source_duration_ms: int = Field(gt=0)
    reconciliation: SceneReconciliation

    @model_validator(mode="after")
    def _duration(self) -> RenderTimelineScene:
        r = self.reconciliation
        if self.timeline_duration_ms != self.source_duration_ms - r.trim_ms + r.freeze_ms:
            raise ValueError(
                f"scene {self.scene_id}: timeline_duration_ms must equal "
                "source_duration_ms - trim_ms + freeze_ms"
            )
        return self


class RenderVoicePlacement(FrozenModel):
    """台本シーン1件のナレーション音声の配置。"""

    script_scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    source_voice: ArtifactDigestRef
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    storyboard_scene_ids: Annotated[
        tuple[Annotated[str, Field(pattern=STORYBOARD_SCENE_ID_PATTERN)], ...],
        Field(min_length=1),
    ]

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


class SubtitleCue(FrozenModel):
    """字幕1枚。本文は持たない。

    台本シーンのナレーションへの文字オフセット ``[char_start, char_end)`` だけを持つ。
    """

    cue_index: int = Field(ge=0)
    script_scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _ranges(self) -> SubtitleCue:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be > char_start")
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be > start_ms")
        return self


def _check_timeline(
    scenes: tuple[RenderTimelineScene, ...],
    voices: tuple[RenderVoicePlacement, ...],
    cues: tuple[SubtitleCue, ...],
    total_duration_ms: int,
) -> None:
    """計画と完成動画が共有する内部整合性（ADR-0019 §4 / §8）。"""
    expected_start = 0
    scene_ids: set[str] = set()
    for index, scene in enumerate(scenes, start=1):
        if scene.order != index:
            raise ValueError(f"scene orders must be 1..N consecutive; got {scene.order}")
        if scene.scene_id in scene_ids:
            raise ValueError(f"duplicate scene_id {scene.scene_id}")
        scene_ids.add(scene.scene_id)
        if scene.timeline_start_ms != expected_start:
            raise ValueError(
                f"scene {scene.scene_id} must start at {expected_start} ms "
                f"(no gaps / overlaps), got {scene.timeline_start_ms}"
            )
        expected_start += scene.timeline_duration_ms
    if expected_start != total_duration_ms:
        raise ValueError(f"sum of scene durations {expected_start} != total {total_duration_ms}")

    windows: dict[str, RenderVoicePlacement] = {}
    previous_end = 0
    for voice in voices:
        if voice.script_scene_id in windows:
            raise ValueError(f"duplicate voice for {voice.script_scene_id}")
        if voice.start_ms < previous_end:
            raise ValueError(f"voice {voice.script_scene_id} overlaps the previous voice")
        if voice.end_ms > total_duration_ms:
            raise ValueError(f"voice {voice.script_scene_id} ends after the total duration")
        if len(set(voice.storyboard_scene_ids)) != len(voice.storyboard_scene_ids):
            raise ValueError("storyboard_scene_ids must be unique")
        unknown = set(voice.storyboard_scene_ids) - scene_ids
        if unknown:
            raise ValueError(f"voice {voice.script_scene_id} refers to unknown scenes {unknown}")
        windows[voice.script_scene_id] = voice
        previous_end = voice.end_ms

    previous_cue_end = 0
    char_cursor: dict[str, int] = {}
    for index, cue in enumerate(cues):
        if cue.cue_index != index:
            raise ValueError(f"cue_index must be 0..N-1 consecutive; got {cue.cue_index}")
        voice = windows.get(cue.script_scene_id)
        if voice is None:
            raise ValueError(f"cue {index} refers to a script scene without voice")
        if cue.start_ms < previous_cue_end:
            raise ValueError(f"cue {index} overlaps the previous cue")
        if cue.start_ms < voice.start_ms or cue.end_ms > voice.end_ms:
            raise ValueError(f"cue {index} is outside its voice window")
        if cue.char_start < char_cursor.get(cue.script_scene_id, 0):
            raise ValueError(f"cue {index} character range goes backwards")
        char_cursor[cue.script_scene_id] = cue.char_end
        previous_cue_end = cue.end_ms


class RenderPlan(FrozenModel):
    """描画計画。固定した入力 + profile + policy + engine から決定的に作る（保存しない）。"""

    scenes: Annotated[tuple[RenderTimelineScene, ...], Field(min_length=1)]
    voices: Annotated[tuple[RenderVoicePlacement, ...], Field(min_length=1)]
    subtitle_cues: tuple[SubtitleCue, ...]
    total_duration_ms: int = Field(gt=0)
    profile: RenderProfile
    policy: TimelinePolicy
    engine: RenderEngineIdentity

    @model_validator(mode="after")
    def _consistent(self) -> RenderPlan:
        _check_timeline(self.scenes, self.voices, self.subtitle_cues, self.total_duration_ms)
        if not self.profile.subtitles.enabled and self.subtitle_cues:
            raise ValueError("subtitle cues present but subtitles are disabled in the profile")
        return self


# --------------------------------------------------------------------------- 完成動画


class FinalMediaDescriptor(FrozenModel):
    """完成動画本体の所在と指紋。キーは ``domain.artifact.keys`` の規約。"""

    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    bytes: int = Field(gt=0, le=FINAL_VIDEO_MAX_BYTES)
    mime: Literal["video/mp4"]


class FinalVideoMeasured(FrozenModel):
    """保存した完成動画を実際にデコードして測った値。"""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    fps_millis: int = Field(gt=0)
    video_codec: str = Field(min_length=1, max_length=32)
    pix_fmt: str = Field(min_length=1, max_length=32)
    audio_present: bool
    audio_codec: str | None = Field(default=None, min_length=1, max_length=32)
    audio_sample_rate_hz: int | None = Field(default=None, gt=0)
    audio_channels: int | None = Field(default=None, ge=1, le=8)

    @model_validator(mode="after")
    def _audio(self) -> FinalVideoMeasured:
        fields = (self.audio_codec, self.audio_sample_rate_hz, self.audio_channels)
        if self.audio_present != all(v is not None for v in fields):
            raise ValueError("audio fields must be set iff audio_present")
        if not self.audio_present and any(v is not None for v in fields):
            raise ValueError("audio fields must be empty when audio is absent")
        return self


class TechnicalQaCheck(FrozenModel):
    check: str = Field(pattern=QA_CHECK_NAME_PATTERN)
    passed: bool
    detail: str = Field(max_length=500)


class TechnicalQaReport(FrozenModel):
    """技術検査の結果。完成動画は**合格したときだけ**保存するので ``passed`` は常に True。"""

    passed: Literal[True]
    checks: Annotated[tuple[TechnicalQaCheck, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _all_passed(self) -> TechnicalQaReport:
        failed = [c.check for c in self.checks if not c.passed]
        if failed:
            raise ValueError(f"technical QA checks failed: {failed}")
        names = [c.check for c in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("duplicate technical QA check name")
        return self


class FinalVideoArtifact(FrozenModel):
    """完成動画（ADR-0019）。計画の時間軸・音声配置・字幕 cue を複写して自己完結させる。"""

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.FINAL_VIDEO]
    schema_version: Literal["1.0"]
    source_production_manifest: SourceArtifactRef
    source_script: SourceArtifactRef
    source_storyboard: SourceArtifactRef
    render_profile: RenderProfile
    render_policy: TimelinePolicy
    render_plan_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    render_engine: RenderEngineIdentity
    template_version: int = Field(ge=1)
    media: FinalMediaDescriptor
    measured: FinalVideoMeasured
    total_duration_ms: int = Field(gt=0)
    timeline: Annotated[tuple[RenderTimelineScene, ...], Field(min_length=1)]
    voice_placements: Annotated[tuple[RenderVoicePlacement, ...], Field(min_length=1)]
    subtitle_cues: tuple[SubtitleCue, ...]
    technical_qa: TechnicalQaReport

    @model_validator(mode="after")
    def _consistent(self) -> FinalVideoArtifact:
        _check_timeline(
            self.timeline, self.voice_placements, self.subtitle_cues, self.total_duration_ms
        )
        profile = self.render_profile
        if (self.measured.width, self.measured.height) != (profile.width, profile.height):
            raise ValueError("measured resolution does not match the render profile")
        tolerance = profile.limits.duration_tolerance_ms
        if abs(self.measured.duration_ms - self.total_duration_ms) > tolerance:
            raise ValueError("measured duration differs from the timeline beyond tolerance")
        if profile.limits.audio_required and not self.measured.audio_present:
            raise ValueError("the render profile requires audio")
        if self.template_version != self.render_policy.template_version:
            raise ValueError("template_version must match render_policy.template_version")
        return self


__all__ = [
    "DEFAULT_RENDER_CONCURRENCY",
    "DEFAULT_RENDER_FFMPEG_THREADS",
    "DEFAULT_RENDER_HEARTBEAT_TIMEOUT_SECONDS",
    "DEFAULT_RENDER_MAX_FREEZE_MS",
    "DEFAULT_RENDER_MIN_FREE_BYTES",
    "DEFAULT_RENDER_PROFILE_ID",
    "DEFAULT_RENDER_TIMEOUT_SECONDS",
    "FINAL_VIDEO_MAX_BYTES",
    "FINAL_VIDEO_MIME_TYPE",
    "RENDER_ARTIFACT_SCHEMA_VERSION",
    "RENDER_PROFILES",
    "RENDER_TEMPLATE_VERSION",
    "FinalMediaDescriptor",
    "FinalVideoArtifact",
    "FinalVideoMeasured",
    "RenderAudioSettings",
    "RenderEngineIdentity",
    "RenderLimits",
    "RenderPlan",
    "RenderProfile",
    "RenderSubtitleSettings",
    "RenderTimelineScene",
    "RenderVideoSettings",
    "RenderVoicePlacement",
    "SceneReconciliation",
    "SubtitleCue",
    "TechnicalQaCheck",
    "TechnicalQaReport",
    "TimelinePolicy",
    "get_render_profile",
]
