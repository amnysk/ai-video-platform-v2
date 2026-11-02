"""台本・storyboard・ブランド方針から ``MusicRequirements`` を決める（ADR-0087 §5）。純粋関数。

- LLM を使わない。語彙表（``lexicon``）と数値だけで決まるので、同じ入力は同じ要件
- ``matched_terms`` は語彙表の key だけ（原文の語・台本の文は出さない。ログ・DB にそのまま出せる）
- 要件の正準 hash（``requirements_sha256``）が収集の需要の識別子になる
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from contracts.music import Energy, VideoFormat
from contracts.music_selection import (
    BrandMusicPolicy,
    Culture,
    MusicRequirements,
    NarrationDensity,
    TempoBand,
    Tension,
)
from domain.music.lexicon import (
    CULTURE_PRIORITY,
    DEFAULT_MOOD,
    MOOD_PRIORITY,
    TENSION_ORDER,
    match_entries,
)

#: ナレーション密度の境（1 秒あたりの文字数 / 語数 × 1000）。日本語は文字、英語は語で数える
_DENSITY_BOUNDS_PER_MILLE: dict[str, tuple[int, int]] = {
    "ja": (4_000, 7_000),
    "en": (1_800, 3_000),
}
#: 1 分あたりの場面数の境
_FAST_CUTS_PER_MINUTE = 8
_SLOW_CUTS_PER_MINUTE = 4
_MAX_SECONDARY_MOODS = 2


def requirements_sha256(requirements: MusicRequirements) -> str:
    """要件の正準 JSON の sha256（収集の需要の識別子。キー順・空白に依らない）。"""
    body = json.dumps(
        requirements.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _density(script: ScriptArtifact) -> NarrationDensity:
    text = script.narration
    units = len(text.split()) if script.language == "en" else len("".join(text.split()))
    per_mille = units * 1_000_000 // max(script.total_duration_ms, 1)
    sparse, dense = _DENSITY_BOUNDS_PER_MILLE[script.language]
    if per_mille < sparse:
        return "sparse"
    return "dense" if per_mille >= dense else "normal"


def _energy(tension: Tension, cuts_per_minute: int) -> Energy:
    if tension == "high" or cuts_per_minute >= _FAST_CUTS_PER_MINUTE:
        return "high"
    if tension == "low" and cuts_per_minute <= _SLOW_CUTS_PER_MINUTE:
        return "low"
    return "medium"


_TEMPO: dict[Energy, TempoBand] = {"low": "slow", "medium": "moderate", "high": "fast"}


def derive_requirements(
    script: ScriptArtifact,
    storyboard: StoryboardArtifact,
    *,
    fmt: VideoFormat,
    brand: BrandMusicPolicy,
) -> MusicRequirements:
    text = "\n".join((script.title, script.hook, script.metadata.topic, script.narration))
    matched = match_entries(text)

    # mood: 当たった語彙の数 → 優先順。ブランドが避ける mood は選ばない
    votes: Counter[str] = Counter(e.mood for e in matched if e.mood is not None)
    avoided = set(brand.avoided_moods)
    ranked_moods = sorted(
        (m for m in votes if m not in avoided),
        key=lambda m: (-votes[m], MOOD_PRIORITY.index(m) if m in MOOD_PRIORITY else 99, m),
    )
    if ranked_moods:
        mood, secondary = ranked_moods[0], tuple(ranked_moods[1 : 1 + _MAX_SECONDARY_MOODS])
    else:
        fallback = [m for m in brand.preferred_moods if m not in avoided]
        mood = fallback[0] if fallback else DEFAULT_MOOD
        secondary = ()

    tension: Tension = "medium"
    seen_tensions: list[Tension] = [e.tension for e in matched if e.tension is not None]
    if seen_tensions:
        tension = max(seen_tensions, key=lambda t: TENSION_ORDER[t])

    cultures: set[Culture] = {e.culture for e in matched if e.culture is not None}
    culture: Culture = "neutral"
    picked: Culture | None = next((c for c in CULTURE_PRIORITY if c in cultures), None)
    if picked is not None:
        culture = picked
    elif brand.preferred_cultures:
        culture = brand.preferred_cultures[0]

    era = next((e.era for e in matched if e.era is not None), None)

    cuts = len(storyboard.scenes) * 60_000 // max(storyboard.total_duration_ms, 1)
    energy = _energy(tension, cuts)

    return MusicRequirements(
        brand_id=brand.brand_id,
        mood=mood,
        secondary_moods=secondary,
        energy=energy,
        tension=tension,
        tempo_band=_TEMPO[energy],
        culture=culture,
        era=era,
        narration_density=_density(script),
        scene_changes_per_minute=cuts,
        duration_ms=script.total_duration_ms,
        fmt=fmt,
        matched_terms=tuple(e.key for e in matched),
    )


__all__ = ["derive_requirements", "requirements_sha256"]
