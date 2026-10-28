"""承認済み BGM の選別（INV-97）。純粋関数。時計も I/O も持たない。

法的な事実（承認・期限・プラットフォーム・形式・再利用・商用・改変・帰属文）は**除外条件**で、
どんな優先度でも覆らない。mood・直近の使用・尺は**ソフトな並び順**で、合法な曲を除外しない。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from contracts.music import BgmTrack, Platform, VideoFormat


class RejectReason(StrEnum):
    LICENSE_NOT_APPROVED = "license_not_approved"
    TRACK_NOT_ACTIVE = "track_not_active"
    EXPIRED = "expired"
    REUSE_NOT_ALLOWED = "reuse_not_allowed"
    COMMERCIAL_NOT_ALLOWED = "commercial_not_allowed"
    MODIFICATION_NOT_ALLOWED = "modification_not_allowed"
    PLATFORM_NOT_ALLOWED = "platform_not_allowed"
    FORMAT_NOT_ALLOWED = "format_not_allowed"
    ATTRIBUTION_TEXT_MISSING = "attribution_text_missing"


@dataclass(frozen=True, slots=True)
class Rejection:
    track_id: str
    reasons: tuple[RejectReason, ...]


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """``candidates`` は優先順。空なら呼び出し側は収集依頼を積む（日次では検索しない）。"""

    candidates: tuple[BgmTrack, ...]
    rejected: tuple[Rejection, ...]


def reject_reasons(
    track: BgmTrack, *, fmt: VideoFormat, platform: Platform, now: datetime
) -> tuple[RejectReason, ...]:
    out: list[RejectReason] = []
    if track.license_status != "approved":
        out.append(RejectReason.LICENSE_NOT_APPROVED)
    if track.status != "active":
        out.append(RejectReason.TRACK_NOT_ACTIVE)
    if track.valid_until is not None and track.valid_until <= now:
        out.append(RejectReason.EXPIRED)
    if not track.reuse_allowed:
        out.append(RejectReason.REUSE_NOT_ALLOWED)
    if not track.commercial_use_allowed:
        out.append(RejectReason.COMMERCIAL_NOT_ALLOWED)
    if not track.modification_allowed:
        out.append(RejectReason.MODIFICATION_NOT_ALLOWED)
    if platform not in track.allowed_platforms:
        out.append(RejectReason.PLATFORM_NOT_ALLOWED)
    if fmt not in track.allowed_formats:
        out.append(RejectReason.FORMAT_NOT_ALLOWED)
    # 契約が弾く不整合だが、model_construct 等で迂回された値も選ばない（多層防御）。
    if track.attribution_required and not (track.exact_attribution_text or "").strip():
        out.append(RejectReason.ATTRIBUTION_TEXT_MISSING)
    return tuple(out)


def select_approved(
    catalog: Iterable[BgmTrack],
    *,
    mood: str,
    fmt: VideoFormat,
    platform: Platform,
    duration_ms: int,
    now: datetime,
    recent_usage: Mapping[str, int],
) -> SelectionResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    legal: list[BgmTrack] = []
    rejected: list[Rejection] = []
    for track in catalog:
        reasons = reject_reasons(track, fmt=fmt, platform=platform, now=now)
        if reasons:
            rejected.append(Rejection(track.track_id, reasons))
        else:
            legal.append(track)

    def preference(track: BgmTrack) -> tuple[int, int, int, str]:
        mood_miss = 0 if mood in track.mood_tags else 1
        used = recent_usage.get(track.track_id, 0)
        # 尺が足りない曲はループで足りるので除外しない。足りる曲を僅かに優先するだけ。
        short = 0 if track.duration_ms >= duration_ms else 1
        return (mood_miss, used, short, track.track_id)

    legal.sort(key=preference)
    rejected.sort(key=lambda r: r.track_id)
    return SelectionResult(tuple(legal), tuple(rejected))
