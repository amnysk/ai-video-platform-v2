"""収集の検索語（ADR-0087 §3）。純関数・決定的・語彙は固定表（自由入力を検索構文へ流さない）。

Commons の検索構文: ``filetype:audio incategory:"CC-Zero" <語>``。CC0 のカテゴリに絞るのは
候補の無駄を減らすためで、ライセンスの判断はここでは行わない（``decide_license`` が API 応答で判定する）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from contracts.music_sources import ACQUISITION_MAX_SEARCHES_PER_RUN

BASE = 'filetype:audio incategory:"CC-Zero"'

MOOD_TERMS: dict[str, str] = {
    "calm": "ambient",
    "peaceful": "ambient",
    "serene": "ambient",
    "melancholy": "piano",
    "sad": "piano",
    "nostalgic": "piano",
    "tense": "dark ambient",
    "dark": "dark ambient",
    "mysterious": "drone",
    "epic": "orchestral",
    "uplifting": "acoustic",
    "happy": "acoustic",
    "solemn": "choir",
}
CULTURE_TERMS: dict[str, str] = {
    "japanese": "shakuhachi",
    "east_asian": "guzheng",
    "western": "piano",
    "neutral": "instrumental",
}
ENERGY_TERMS: dict[str, str] = {"low": "ambient", "medium": "instrumental", "high": "percussion"}
TEMPO_TERMS: dict[str, str] = {"slow": "slow", "moderate": "instrumental", "fast": "upbeat"}


def _lookup(table: Mapping[str, str], value: Any) -> str | None:
    return table.get(value) if isinstance(value, str) else None


def build_search_queries(requirements: Mapping[str, Any]) -> list[str]:
    """要件（``MusicRequirements`` 形の dict）から検索語を最大 ``ACQUISITION_MAX_SEARCHES_PER_RUN`` 件。"""
    mood = _lookup(MOOD_TERMS, requirements.get("mood"))
    culture = _lookup(CULTURE_TERMS, requirements.get("culture"))
    energy = _lookup(ENERGY_TERMS, requirements.get("energy"))
    tempo = _lookup(TEMPO_TERMS, requirements.get("tempo_band", requirements.get("tempo")))
    terms: list[str] = []
    if mood and culture:
        terms.append(f"{mood} {culture}")
    for t in (mood, culture, energy, tempo):
        if t:
            terms.append(t)
    terms.append("music")
    out: list[str] = []
    for term in terms:
        query = f"{BASE} {term}"
        if query not in out:
            out.append(query)
    return out[:ACQUISITION_MAX_SEARCHES_PER_RUN]


__all__ = ["build_search_queries"]
