"""独立レビュー（verify 枝）の所見を示す失敗テスト。strict xfail なので修正されると XPASS で気づく。

実データの影の評価（17 本の本番 Episode）で、mood が全く合わない曲が最低点ちょうどで選ばれた。
"""

from __future__ import annotations

import pytest

from contracts.music_selection import (
    DEFAULT_BRAND_POLICY,
    MIN_SELECTION_SCORE,
    MusicRequirements,
)
from domain.music.scoring import score_candidates
from tests.support.bgm_auto import entry_for, loud_track
from tests.support.bgm_catalog import NOW


@pytest.mark.xfail(
    strict=True,
    reason=(
        "P1 review finding: the non-mood items alone can reach 525 (>= MIN_SELECTION_SCORE=400),"
        " so a track whose mood does not match and whose energy is 2 steps away is still"
        " selected; BELOW_MIN_SCORE (no BGM) is nearly unreachable once any approved track exists"
    ),
)
def test_a_track_with_no_mood_match_and_opposite_energy_is_below_the_minimum() -> None:
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
    assert ranked and ranked[0].total < MIN_SELECTION_SCORE
