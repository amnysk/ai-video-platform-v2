"""BGM の採否と混合計画（ADR-0077、INV-113 / INV-114）。純粋関数。時計・I/O を持たない。

- 選曲そのものは ``domain.music``（Wave 1）が行う。ここは cue sheet が指す曲を**同じ検査**
  （``reject_reasons`` / ``credits_for``）で再検査し、1つでも通らなければ BGM 全体を使わない
  （一部の cue だけ鳴る動画は演出意図を壊す。全部使うか、全く使わないか）
- 使わない場合も理由コードを返す。黙って使うことも、黙って落とすことも無い
- 使わない場合の描画は BGM 無しと**同一**（input_hash にも ffmpeg コマンドにも BGM は現れない）
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from contracts.music import BgmTrack, MusicCueSheet
from contracts.music_render import (
    BGM_NARRATION_PRIORITY_MAX_DB,
    BGM_VIDEO_FORMAT_BY_PROFILE_ID,
    BgmEvidenceTrack,
    BgmMixCue,
    BgmMixPlan,
)
from contracts.render import RenderPlan
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.music.credits import CreditsError, credits_for
from domain.music.selection import reject_reasons


class BgmSkipReason(StrEnum):
    NO_CUE_SHEET = "no_cue_sheet"
    UNKNOWN_PROFILE_FORMAT = "unknown_profile_format"
    UNKNOWN_TRACK = "unknown_track"
    LICENSE_REJECTED = "license_rejected"
    CREDITS_INVALID = "credits_invalid"
    FILE_MISSING_OR_ALTERED = "file_missing_or_altered"
    CUE_OUTSIDE_TIMELINE = "cue_outside_timeline"
    UNKNOWN_SCENE = "unknown_scene"
    SOURCE_UNREADABLE = "source_unreadable"


@dataclass(frozen=True, slots=True)
class BgmRejection:
    reason: BgmSkipReason
    track_id: str | None
    #: 機械可読の詳細（``RejectReason`` の値など）。説明欄・ログへそのまま出してよい固定語彙
    detail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BgmResolution:
    """``applied=True`` のときだけ ``plan`` 等が入る。"""

    applied: bool
    plan: BgmMixPlan | None = None
    tracks: tuple[BgmEvidenceTrack, ...] = ()
    credit_lines: tuple[str, ...] = ()
    rejections: tuple[BgmRejection, ...] = ()

    @property
    def identity(self) -> dict[str, object] | None:
        """render_input_hash へ渡す値。使わないなら ``None``（= BGM 無しと同じ hash）。"""
        return self.plan.identity_payload() if self.applied and self.plan is not None else None

    @property
    def mix_plan_sha256(self) -> str:
        if self.plan is None:
            raise ValueError("no mix plan")
        return sha256_hex(canonical_json_bytes(self.plan.identity_payload()))


def _skipped(*rejections: BgmRejection) -> BgmResolution:
    return BgmResolution(applied=False, rejections=tuple(rejections))


def skipped_for(reason: BgmSkipReason) -> BgmResolution:
    return _skipped(BgmRejection(reason, None))


def resolve_bgm(
    cue_sheet: MusicCueSheet | None,
    catalog: Iterable[BgmTrack],
    *,
    plan: RenderPlan,
    now: datetime,
    file_sha256_by_track: Mapping[str, str | None],
) -> BgmResolution:
    """cue sheet を再検査して混合計画にする。通らなければ ``applied=False`` と理由。

    ``file_sha256_by_track``: 実ファイルを読んで求めた sha256（読めなければ None / 欠落）。
    カタログの ``file_sha256`` と cue の ``source_sha256`` の両方に一致しなければ使わない。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if cue_sheet is None:
        return skipped_for(BgmSkipReason.NO_CUE_SHEET)
    fmt = BGM_VIDEO_FORMAT_BY_PROFILE_ID.get(plan.profile.profile_id)
    if fmt is None:
        return skipped_for(BgmSkipReason.UNKNOWN_PROFILE_FORMAT)

    by_id = {t.track_id: t for t in catalog}
    script_scenes = {v.script_scene_id for v in plan.voices}
    rejections: list[BgmRejection] = []
    seen: set[str] = set()
    for cue in cue_sheet.cues:
        track = by_id.get(cue.track_id)
        if track is None:
            rejections.append(BgmRejection(BgmSkipReason.UNKNOWN_TRACK, cue.track_id))
            continue
        if cue.track_id not in seen:
            seen.add(cue.track_id)
            reasons = reject_reasons(track, now=now, fmt=fmt, platform="youtube")
            if reasons:
                rejections.append(
                    BgmRejection(
                        BgmSkipReason.LICENSE_REJECTED,
                        cue.track_id,
                        tuple(r.value for r in reasons),
                    )
                )
            actual = file_sha256_by_track.get(cue.track_id)
            if actual is None or actual != track.file_sha256 or actual != cue.source_sha256:
                rejections.append(BgmRejection(BgmSkipReason.FILE_MISSING_OR_ALTERED, cue.track_id))
        if cue.scene_id not in script_scenes:
            rejections.append(BgmRejection(BgmSkipReason.UNKNOWN_SCENE, cue.track_id))
        if cue.end_ms > plan.total_duration_ms:
            rejections.append(BgmRejection(BgmSkipReason.CUE_OUTSIDE_TIMELINE, cue.track_id))
    credit_lines: tuple[str, ...] = ()
    if not rejections:
        try:
            credit_lines = credits_for(cue_sheet, by_id.values(), now=now)
        except CreditsError:
            rejections.append(BgmRejection(BgmSkipReason.CREDITS_INVALID, None))
    if rejections:
        return _skipped(*rejections)

    mix = BgmMixPlan(
        cues=tuple(
            BgmMixCue(
                scene_id=c.scene_id,
                track_id=c.track_id,
                source_sha256=c.source_sha256,
                license_evidence_hash=c.license_evidence_hash,
                start_ms=c.start_ms,
                end_ms=c.end_ms,
                fade_in_ms=c.fade_in_ms,
                fade_out_ms=c.fade_out_ms,
                level_db=min(c.target_relative_level_db, BGM_NARRATION_PRIORITY_MAX_DB),
                ducking=c.ducking_policy,
            )
            for c in cue_sheet.cues
        )
    )
    tracks: dict[str, BgmEvidenceTrack] = {}
    for cue in cue_sheet.cues:
        tracks.setdefault(
            cue.track_id,
            BgmEvidenceTrack(
                track_id=cue.track_id,
                source_sha256=cue.source_sha256,
                license_evidence_hash=cue.license_evidence_hash,
            ),
        )
    return BgmResolution(
        applied=True, plan=mix, tracks=tuple(tracks.values()), credit_lines=credit_lines
    )


__all__ = [
    "BgmRejection",
    "BgmResolution",
    "BgmSkipReason",
    "resolve_bgm",
    "skipped_for",
]
