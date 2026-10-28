"""BGM カタログと cue sheet の契約（ADR-0072）。

- ここが BGM の語彙・形の**唯一の定義**。``domain/music`` はこれだけを import する
- 実際のライセンス判断はここでは行わない。人が証拠を見て ``license_status`` を決め、
  このモデルはその結果と根拠の hash を**運ぶだけ**
- cue sheet は production の Artifact（画像・音声・動画）を参照しない（INV-100）。
  BGM だけを替えても production 側の入力 hash が動かない
- cue sheet の数値は int（``target_relative_level_db`` は dB の整数単位）。float は正準 JSON の
  sha256 を揺らす（ADR-0019 の ``gain_permille`` と同じ理由）
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from contracts.artifact_refs import (
    SCRIPT_SCENE_ID_PATTERN,
    SHA256_HEX_PATTERN,
    FrozenModel,
    check_canonical_uuid,
)

MUSIC_CONTRACT_SCHEMA_VERSION = "1.0"

BGM_TRACK_ID_PATTERN = r"^bgm-[A-Za-z0-9._-]+$"

SourceKind = Literal["youtube_audio_library", "licensed_external", "original"]
LicenseType = Literal["youtube_audio_library_standard", "cc_by", "custom"]
LicenseStatus = Literal["approved", "unverified", "revoked", "expired"]
Platform = Literal["youtube"]
VideoFormat = Literal["shorts", "longform"]
Energy = Literal["low", "medium", "high"]
TrackStatus = Literal["active", "quarantined", "retired"]
DuckingPolicy = Literal["duck_under_voice", "none"]

#: BGM の相対音量（声を基準 0 とする dB 整数）の形だけの上下限。校正した閾値ではない
#: （実素材での校正は後続。設計 v3 §4.3）。
LEVEL_DB_MIN = -96
LEVEL_DB_MAX = 0


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must be timezone-aware")
    return value


class BgmTrack(FrozenModel):
    track_id: str = Field(pattern=BGM_TRACK_ID_PATTERN)
    file_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    object_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    artist: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_kind: SourceKind
    license_type: LicenseType
    license_status: LicenseStatus
    license_evidence_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    license_checked_at: datetime | None = None
    allowed_platforms: tuple[Platform, ...]
    allowed_formats: tuple[VideoFormat, ...]
    commercial_use_allowed: bool
    reuse_allowed: bool
    modification_allowed: bool
    attribution_required: bool
    exact_attribution_text: str | None = None
    valid_until: datetime | None = None
    mood_tags: tuple[str, ...] = ()
    genre_tags: tuple[str, ...] = ()
    energy: Energy
    bpm: int | None = Field(default=None, gt=0)
    duration_ms: int = Field(gt=0)
    loudness_lufs: float | None = None
    status: TrackStatus

    @field_validator("license_checked_at", "valid_until")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @field_validator("object_key")
    @classmethod
    def _relative_key(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("object_key must be a relative key without '..'")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> BgmTrack:
        text = self.exact_attribution_text
        if self.attribution_required:
            if text is None or not text.strip():
                raise ValueError("attribution_required needs a non-empty exact_attribution_text")
        elif text is not None:
            raise ValueError("exact_attribution_text must be None unless attribution_required")
        if self.license_status == "approved" and (
            self.license_evidence_sha256 is None or self.license_checked_at is None
        ):
            raise ValueError("approved needs license_evidence_sha256 and license_checked_at")
        return self


class MusicCue(FrozenModel):
    scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    track_id: str = Field(pattern=BGM_TRACK_ID_PATTERN)
    source_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt
    fade_in_ms: StrictInt = Field(ge=0)
    fade_out_ms: StrictInt = Field(ge=0)
    target_relative_level_db: StrictInt = Field(ge=LEVEL_DB_MIN, le=LEVEL_DB_MAX)
    ducking_policy: DuckingPolicy
    license_evidence_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    credits_text: str | None = None

    @model_validator(mode="after")
    def _timeline(self) -> MusicCue:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        if self.fade_in_ms + self.fade_out_ms > self.end_ms - self.start_ms:
            raise ValueError("fade_in_ms + fade_out_ms must fit in the cue")
        return self


class MusicCueSheet(FrozenModel):
    schema_version: Literal["1.0"] = MUSIC_CONTRACT_SCHEMA_VERSION
    cues: tuple[MusicCue, ...] = Field(min_length=1)


class BgmUsage(FrozenModel):
    """Episode ごとの BGM 使用証跡。同じ曲を複数 Episode で使っても別々に残る（T12）。"""

    episode_id: str
    track_id: str = Field(pattern=BGM_TRACK_ID_PATTERN)
    license_evidence_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    final_video_sha256: str = Field(pattern=SHA256_HEX_PATTERN)

    @field_validator("episode_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        return check_canonical_uuid(value)


__all__ = [
    "BGM_TRACK_ID_PATTERN",
    "LEVEL_DB_MAX",
    "LEVEL_DB_MIN",
    "MUSIC_CONTRACT_SCHEMA_VERSION",
    "BgmTrack",
    "BgmUsage",
    "MusicCue",
    "MusicCueSheet",
]
