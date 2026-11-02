"""承認済みの曲に項目ごとの点を付ける（ADR-0087 §5、INV-153）。純粋関数。

- 対象は法的な門（``reject_reasons``。INV-97）を通った曲だけ。``policy`` 承認は規則の版が
  今と一致するものだけ（INV-151 の選曲側）。ブランドが許可していない曲・ラウドネス未測定
  （配置できない）の曲も出ない。隔離・未検証の曲は決して点を付けない
- 点は項目ごとの加点（1/1000 単位の int）。合計と内訳を ``ScoreBreakdown`` で返す。
  同点は ``track_id``
- 重みは初期値で実データで校正していない（ADR-0087 Consequences）。変えたら
  ``BGM_SELECTION_RULE_VERSION`` を上げる
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from contracts.music import BgmTrack
from contracts.music_selection import MusicRequirements, ScoreBreakdown, ScoreItem, TempoBand
from contracts.music_sources import BGM_LICENSE_POLICY_VERSION
from domain.music.ports import CatalogEntry
from domain.music.selection import reject_reasons

WEIGHT_MOOD_PRIMARY = 300
WEIGHT_MOOD_SECONDARY = 150
WEIGHT_ENERGY_EXACT = 150
WEIGHT_ENERGY_ADJACENT = 75
WEIGHT_TEMPO_EXACT = 100
WEIGHT_TEMPO_ADJACENT = 50
WEIGHT_TEMPO_UNKNOWN = 50
WEIGHT_CULTURE_MATCH = 100
WEIGHT_CULTURE_NEUTRAL = 50
WEIGHT_DURATION_MAX = 100
WEIGHT_FRESHNESS_MAX = 100
WEIGHT_FRESHNESS_PER_USE = 25
WEIGHT_RISK = {"low": 100, "unknown": 50, "elevated": 0}
WEIGHT_BRAND_OK = 100

_ENERGY_RANK = {"low": 0, "medium": 1, "high": 2}
_TEMPO_RANK: dict[TempoBand, int] = {"slow": 0, "moderate": 1, "fast": 2}
_SLOW_BPM_BELOW = 80
_FAST_BPM_FROM = 110


def _tempo_band(bpm: int) -> TempoBand:
    if bpm < _SLOW_BPM_BELOW:
        return "slow"
    return "fast" if bpm >= _FAST_BPM_FROM else "moderate"


def eligible(entry: CatalogEntry, requirements: MusicRequirements, *, now: datetime) -> bool:
    """選曲の対象か。隔離・未検証・期限切れ・版違いの自動承認・ブランド不許可は対象外。"""
    track = entry.track
    if reject_reasons(track, now=now, fmt=requirements.fmt, platform="youtube"):
        return False
    if (
        track.license_approver_kind == "policy"
        and track.license_policy_version != BGM_LICENSE_POLICY_VERSION
    ):
        return False
    if track.allowed_brands and requirements.brand_id not in track.allowed_brands:
        return False
    return track.loudness_lufs is not None


def _items(
    track: BgmTrack, req: MusicRequirements, used: int, *, avoided: frozenset[str]
) -> tuple[tuple[ScoreItem, int], ...]:
    moods = set(track.mood_tags)
    if req.mood in moods:
        mood = WEIGHT_MOOD_PRIMARY
    elif moods & set(req.secondary_moods):
        mood = WEIGHT_MOOD_SECONDARY
    else:
        mood = 0

    gap = abs(_ENERGY_RANK[track.energy] - _ENERGY_RANK[req.energy])
    energy = WEIGHT_ENERGY_EXACT if gap == 0 else WEIGHT_ENERGY_ADJACENT if gap == 1 else 0

    if track.bpm is None:
        tempo = WEIGHT_TEMPO_UNKNOWN
    else:
        tgap = abs(_TEMPO_RANK[_tempo_band(track.bpm)] - _TEMPO_RANK[req.tempo_band])
        tempo = WEIGHT_TEMPO_EXACT if tgap == 0 else WEIGHT_TEMPO_ADJACENT if tgap == 1 else 0

    if req.culture == "neutral":
        culture = WEIGHT_CULTURE_NEUTRAL
    else:
        tags = {t.casefold() for t in (*track.mood_tags, *track.genre_tags)}
        culture = WEIGHT_CULTURE_MATCH if req.culture in tags else 0

    duration = min(WEIGHT_DURATION_MAX, WEIGHT_DURATION_MAX * track.duration_ms // req.duration_ms)
    freshness = max(0, WEIGHT_FRESHNESS_MAX - WEIGHT_FRESHNESS_PER_USE * used)
    risk = WEIGHT_RISK[track.content_id_risk]
    brand = 0 if moods & avoided else WEIGHT_BRAND_OK
    return (
        (ScoreItem.MOOD, mood),
        (ScoreItem.ENERGY, energy),
        (ScoreItem.TEMPO, tempo),
        (ScoreItem.CULTURE, culture),
        (ScoreItem.DURATION, duration),
        (ScoreItem.FRESHNESS, freshness),
        (ScoreItem.CONTENT_ID_RISK, risk),
        (ScoreItem.BRAND, brand),
    )


def score_candidates(
    requirements: MusicRequirements,
    entries: Iterable[CatalogEntry],
    recent_usage: Mapping[str, int],
    now: datetime,
    *,
    avoided_moods: Iterable[str] = (),
) -> tuple[ScoreBreakdown, ...]:
    """対象の曲を点の高い順（同点は ``track_id`` 昇順）に返す。入力の並びに依らない。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    avoided = frozenset(avoided_moods)
    out: list[ScoreBreakdown] = []
    for entry in entries:
        if not eligible(entry, requirements, now=now):
            continue
        items = _items(
            entry.track, requirements, recent_usage.get(entry.track.track_id, 0), avoided=avoided
        )
        out.append(
            ScoreBreakdown(
                track_id=entry.track.track_id, items=items, total=sum(p for _, p in items)
            )
        )
    out.sort(key=lambda b: (-b.total, b.track_id))
    return tuple(out)


__all__ = ["eligible", "score_candidates"]
