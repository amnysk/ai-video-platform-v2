"""BGM を Render へ結線するための契約（ADR-0077）。``contracts/music.py`` の語彙を import して使う。

- 既存の ``contracts/music.py`` / ``contracts/render.py`` の定義は変えない。ここは追加だけ
- 混合の定数（BGM の上限レベル・ラウドネスの許容範囲）は**ここが唯一の定義**（AGENTS.md §8）
- 数値はすべて int（dB・LUFS は 1/100 単位）。float は正準 JSON の sha256 を揺らす
- ``BGM_MIX_VERSION`` を上げる条件: ffmpeg の混合 filter（ducking・fade・limiter）の組み方、
  または上限レベルを変えたとき。``MixPlan`` の hash が input_hash に入るので、上げれば再描画される
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt

from contracts.artifact_refs import (
    SCRIPT_SCENE_ID_PATTERN,
    SHA256_HEX_PATTERN,
    FrozenModel,
    check_canonical_uuid,
)
from contracts.music import BGM_TRACK_ID_PATTERN, DuckingPolicy, VideoFormat

BGM_MIX_VERSION = 1
BGM_EVIDENCE_SCHEMA_VERSION = "1.0"
#: 結線を有効にする設定キー（``Settings.bgm_enabled``）。既定は OFF。
BGM_SETTING_KEY = "bgm_enabled"

#: ナレーション優先: cue が何 dB を指定しても、声を基準 0 とした BGM の相対音量はこれ以下にする。
BGM_NARRATION_PRIORITY_MAX_DB = -12

#: ラウドネスの許容範囲（1/100 LUFS・1/100 dBTP）。**実素材で校正した値ではない**（ADR-0077）。
LOUDNESS_INTEGRATED_MIN_CENTI = -2400
LOUDNESS_INTEGRATED_MAX_CENTI = -900
LOUDNESS_TRUE_PEAK_MAX_CENTI = -100

BGM_LOUDNESS_CHECK_NAME = "bgm_loudness"

#: render profile id → BGM のライセンス検査が使う動画形式。ここに無い profile では BGM を使わない
#: （形式を推測しない。Shorts 不可の曲を長尺 profile の名目で通さないため）。
BGM_VIDEO_FORMAT_BY_PROFILE_ID: dict[str, VideoFormat] = {
    "shorts_vertical": "shorts",
    "long_form_horizontal": "longform",
}

LoudnessReason = Literal[
    "ok",
    "too_quiet",
    "too_loud",
    "true_peak_exceeded",
    "silent",
]


class BgmMixCue(FrozenModel):
    """ffmpeg が実際に使う cue（上限レベルを適用済み）。"""

    scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    track_id: str = Field(pattern=BGM_TRACK_ID_PATTERN)
    source_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    license_evidence_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    fade_in_ms: StrictInt = Field(ge=0)
    fade_out_ms: StrictInt = Field(ge=0)
    #: 声を基準 0 とした dB（``BGM_NARRATION_PRIORITY_MAX_DB`` 以下）
    level_db: StrictInt
    ducking: DuckingPolicy


class BgmMixPlan(FrozenModel):
    """BGM の混合計画。**この正準 JSON が render の input_hash に入る**。"""

    mix_version: Literal[1] = BGM_MIX_VERSION
    cues: tuple[BgmMixCue, ...] = Field(min_length=1)

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class LoudnessMeasurement(FrozenModel):
    """完成動画の音声の実測（1/100 単位の int）。無音は ``integrated_lufs_centi=None``。"""

    integrated_lufs_centi: int | None
    true_peak_dbtp_centi: int | None
    lra_centi: int | None


class LoudnessVerdict(FrozenModel):
    passed: bool
    reason_code: LoudnessReason
    measured: LoudnessMeasurement
    integrated_min_centi: int = LOUDNESS_INTEGRATED_MIN_CENTI
    integrated_max_centi: int = LOUDNESS_INTEGRATED_MAX_CENTI
    true_peak_max_centi: int = LOUDNESS_TRUE_PEAK_MAX_CENTI


class BgmEvidenceTrack(FrozenModel):
    track_id: str = Field(pattern=BGM_TRACK_ID_PATTERN)
    source_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    license_evidence_hash: str = Field(pattern=SHA256_HEX_PATTERN)


class BgmRenderEvidence(FrozenModel):
    """完成動画に結び付く BGM の使用証跡（DDL 無し。MinIO の JSON として完成動画と同時に残す）。

    説明欄の agent は ``credit_lines``（カタログ原文）だけを読む。
    """

    schema_version: Literal["1.0"] = BGM_EVIDENCE_SCHEMA_VERSION
    episode_id: str
    input_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    final_video_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    mix_plan_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    tracks: tuple[BgmEvidenceTrack, ...] = Field(min_length=1)
    credit_lines: tuple[str, ...]
    loudness: LoudnessVerdict

    def model_post_init(self, __context: object) -> None:
        check_canonical_uuid(self.episode_id)


__all__ = [
    "BGM_VIDEO_FORMAT_BY_PROFILE_ID",
    "BGM_EVIDENCE_SCHEMA_VERSION",
    "BGM_LOUDNESS_CHECK_NAME",
    "BGM_MIX_VERSION",
    "BGM_NARRATION_PRIORITY_MAX_DB",
    "BGM_SETTING_KEY",
    "LOUDNESS_INTEGRATED_MAX_CENTI",
    "LOUDNESS_INTEGRATED_MIN_CENTI",
    "LOUDNESS_TRUE_PEAK_MAX_CENTI",
    "BgmEvidenceTrack",
    "BgmMixCue",
    "BgmMixPlan",
    "BgmRenderEvidence",
    "LoudnessMeasurement",
    "LoudnessReason",
    "LoudnessVerdict",
]
