"""独立レビュー（verify 枝）の所見を示す失敗テスト。F1 は修正済み（select-2）で、ここは回帰の検査。

実データの影の評価（17 本の本番 Episode）で、mood が全く合わない曲が最低点ちょうどで選ばれた。
"""

from __future__ import annotations

from contracts.music_selection import (
    DEFAULT_BRAND_POLICY,
    MIN_SELECTION_SCORE,
    MusicRequirements,
)
from domain.music.auto_select import is_suitable, ladder_candidates, select_for_episode
from domain.music.scoring import score_candidates
from tests.support.bgm_auto import entry_for, loud_track
from tests.support.bgm_catalog import NOW


def test_a_track_whose_mood_does_not_match_is_never_suitable_even_at_the_minimum_score() -> None:
    # F1（独立レビュー）: mood 以外の項目だけで最低点に届く。点ではなく is_suitable で落とす
    req = MusicRequirements(
        brand_id=DEFAULT_BRAND_POLICY.brand_id,
        mood="tense",
        secondary_moods=(),
        energy="high",
        tension="high",
        tempo_band="fast",
        culture="neutral",
        era=None,
        narration_density="normal",
        scene_changes_per_minute=12,
        duration_ms=40_000,
        fmt="shorts",
        matched_terms=("war",),
    )
    calm = entry_for(
        loud_track("bgm-calm", "a", mood_tags=("calm",), energy="low", duration_ms=200_000)
    )
    ranked = score_candidates(req, [calm], {}, NOW, avoided_moods=())
    assert ranked and ranked[0].total >= MIN_SELECTION_SCORE  # 点だけなら通ってしまう
    assert not is_suitable(ranked[0])
    decision = select_for_episode(
        "00000000-0000-4000-8000-000000000001", req, [calm], {}, NOW, brand=DEFAULT_BRAND_POLICY
    )
    assert decision.track_id is None and decision.acquisition_requested
    assert ladder_candidates(decision, limit=2) == ()
