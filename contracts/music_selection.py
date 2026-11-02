"""Episode 単位の自動選曲と、その決定の永続化の契約（ADR-0087 §5〜§7）。**唯一の定義**。

- 要件（``MusicRequirements``）は台本・storyboard・ブランド方針から純関数で決まる（LLM 不使用）
- 点数は項目ごとの内訳（``ScoreBreakdown``）で残し、なぜその曲かを説明できる
- 決定（``BgmSelectionDecision``）は Episode ごとに 1 回だけ作り ``bgm_selections`` に保存。
  再実行は保存値を使う
- 数値は int（点数は 1/1000 単位）。float は正準 JSON の sha256 を揺らす
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictInt

from contracts.artifact_refs import SHA256_HEX_PATTERN, FrozenModel
from contracts.music import BGM_TRACK_ID_PATTERN, Energy, MusicCueSheet, VideoFormat

#: 選曲の規則の版。要件の導出・点数の重み・フォールバックの梯子を変えたら上げる。
#: 保存済みの決定は版に依らず再利用する（同じ Episode の再実行は同じ曲。INV-153）
#: select-2: 「適切」に mood の一致を要求、cut 数で energy を high にしない（独立レビュー F1 / F2）
BGM_SELECTION_RULE_VERSION = "select-2"
#: 既定のブランド（多ブランド化までの単一値）
DEFAULT_BRAND_ID = "default"

#: これ未満の最上位しか無ければ「適切な曲が無い」とし、収集の需要を積む（1/1000 点）
MIN_SELECTION_SCORE = 400
#: フォールバックの上限（ADR-0087 §7）
FALLBACK_MAX_TRACKS = 2
FALLBACK_GAIN_STEP_DB = -3
FALLBACK_MAX_GAIN_STEPS = 1

Tension = Literal["low", "medium", "high"]
TempoBand = Literal["slow", "moderate", "fast"]
Culture = Literal["japanese", "east_asian", "western", "neutral"]
NarrationDensity = Literal["sparse", "normal", "dense"]


class BrandMusicPolicy(FrozenModel):
    """ブランドの音楽方針。未設定なら ``DEFAULT_BRAND_POLICY``。"""

    brand_id: str = Field(min_length=1, max_length=64)
    preferred_moods: tuple[str, ...] = ()
    avoided_moods: tuple[str, ...] = ()
    preferred_cultures: tuple[Culture, ...] = ()
    #: BGM を付けるか（False なら選曲せず BGM 無し）
    music_enabled: bool = True


DEFAULT_BRAND_POLICY = BrandMusicPolicy(brand_id=DEFAULT_BRAND_ID)


class MusicRequirements(FrozenModel):
    brand_id: str = Field(min_length=1, max_length=64)
    mood: str = Field(min_length=1, max_length=40)
    secondary_moods: tuple[str, ...] = ()
    energy: Energy
    tension: Tension
    tempo_band: TempoBand
    culture: Culture
    era: str | None = Field(default=None, max_length=40)
    narration_density: NarrationDensity
    #: 場面の切り替わり（1 分あたりの scene 数）
    scene_changes_per_minute: StrictInt = Field(ge=0)
    duration_ms: StrictInt = Field(gt=0)
    fmt: VideoFormat
    #: 要件の導出に効いた語彙（説明用。固定語彙の key だけ。原文は入れない）
    matched_terms: tuple[str, ...] = ()


class ScoreItem(StrEnum):
    MOOD = "mood"
    ENERGY = "energy"
    TEMPO = "tempo"
    CULTURE = "culture"
    DURATION = "duration"
    FRESHNESS = "freshness"
    CONTENT_ID_RISK = "content_id_risk"
    BRAND = "brand"


class ScoreBreakdown(FrozenModel):
    track_id: str = Field(pattern=BGM_TRACK_ID_PATTERN)
    #: 項目ごとの点（1/1000）。合計は ``total``
    items: tuple[tuple[ScoreItem, StrictInt], ...]
    total: StrictInt


class FallbackStep(StrEnum):
    SELECTED = "selected"
    GAIN_REDUCED = "gain_reduced"
    NEXT_TRACK = "next_track"
    NO_BGM = "no_bgm"
    NO_BGM_AFTER_LOUDNESS = "no_bgm_after_loudness"


class FallbackReason(StrEnum):
    NO_APPROVED_TRACK = "no_approved_track"
    BELOW_MIN_SCORE = "below_min_score"
    SPEECH_MARGIN_TOO_SMALL = "speech_margin_too_small"
    SPEECH_MARGIN_UNMEASURED = "speech_margin_unmeasured"
    SOURCE_UNREADABLE = "source_unreadable"
    LICENSE_RECHECK_FAILED = "license_recheck_failed"
    PLACEMENT_REFUSED = "placement_refused"
    FINAL_LOUDNESS_OUT_OF_RANGE = "final_loudness_out_of_range"
    BRAND_MUSIC_DISABLED = "brand_music_disabled"


class FallbackEvent(FrozenModel):
    step: FallbackStep
    track_id: str | None = Field(default=None, pattern=BGM_TRACK_ID_PATTERN)
    reason: FallbackReason | None = None
    gain_offset_db: StrictInt = 0
    #: 実測した最小の発話中の差（1/100 LU）。測っていなければ None
    min_speech_margin_centi_lu: StrictInt | None = None


class BgmSelectionDecision(FrozenModel):
    """Episode 1 件の選曲の決定。``bgm_selections`` の 1 行（episode_id UNIQUE）。"""

    episode_id: str
    rule_version: str
    requirements: MusicRequirements
    requirements_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    #: 上位の候補（最大 5）の内訳。選曲の説明
    ranked: tuple[ScoreBreakdown, ...] = ()
    #: 最終的に使う曲（BGM 無しなら None）
    track_id: str | None = Field(default=None, pattern=BGM_TRACK_ID_PATTERN)
    #: 曲の ``mix_gain_offset_db`` に、フォールバックの減衰を足した値
    applied_gain_offset_db: StrictInt = 0
    cue_sheet: MusicCueSheet | None = None
    cue_sheet_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    min_speech_margin_centi_lu: StrictInt | None = None
    fallback: tuple[FallbackEvent, ...] = ()
    #: 適切な曲が無く、収集の需要を積んだか
    acquisition_requested: bool = False


__all__ = [
    "BGM_SELECTION_RULE_VERSION",
    "DEFAULT_BRAND_ID",
    "DEFAULT_BRAND_POLICY",
    "FALLBACK_GAIN_STEP_DB",
    "FALLBACK_MAX_GAIN_STEPS",
    "FALLBACK_MAX_TRACKS",
    "MIN_SELECTION_SCORE",
    "BgmSelectionDecision",
    "BrandMusicPolicy",
    "Culture",
    "FallbackEvent",
    "FallbackReason",
    "FallbackStep",
    "MusicRequirements",
    "NarrationDensity",
    "ScoreBreakdown",
    "ScoreItem",
    "TempoBand",
    "Tension",
]
