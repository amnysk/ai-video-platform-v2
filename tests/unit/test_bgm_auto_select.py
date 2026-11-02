"""要件の導出・採点・Episode 単位の選曲の決定（ADR-0087 §5、INV-153）。純粋関数のテスト。"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from contracts.music_selection import (
    DEFAULT_BRAND_POLICY,
    MIN_SELECTION_SCORE,
    BrandMusicPolicy,
    FallbackReason,
    ScoreItem,
)
from contracts.music_sources import BGM_LICENSE_POLICY_VERSION
from domain.music.auto_select import ladder_candidates, select_for_episode
from domain.music.requirements import derive_requirements, requirements_sha256
from domain.music.scoring import score_candidates
from tests.support.bgm_auto import EPISODE, entry_for, loud_track, make_script, make_storyboard
from tests.support.bgm_catalog import NOW

BRAND = DEFAULT_BRAND_POLICY


def _req(**script_kw):
    script = make_script(**script_kw)
    return derive_requirements(script, make_storyboard(script), fmt="shorts", brand=BRAND)


# ------------------------------------------------------------------ requirements


def test_war_terms_give_high_tension_and_tense_mood_in_japanese_and_english() -> None:
    ja = _req(title="関ヶ原の合戦", narrations=("両軍が激突した。", "決戦だ。", "終わり。"))
    en = _req(title="The Battle of Sekigahara", language="en", narrations=("Armies met.",) * 3)
    for r in (ja, en):
        assert r.tension == "high" and r.mood == "tense" and r.energy == "high"
        assert "war" in r.matched_terms


def test_temple_and_tea_give_calm_japanese_and_era_terms_set_the_era() -> None:
    r = _req(title="江戸の茶室", narrations=("寺で茶を点てる。",) * 3)
    assert r.mood == "calm" and r.tension == "low" and r.culture == "japanese"
    assert r.era == "edo"
    assert {"temple", "tea", "era_edo"} <= set(r.matched_terms)


def test_mystery_terms_and_default_when_nothing_matches() -> None:
    assert _req(title="消えた謎の都市").mood == "mysterious"
    plain = _req()
    assert plain.mood == "calm" and plain.culture == "neutral" and plain.matched_terms == ()


def test_matched_terms_are_lexicon_keys_never_raw_text() -> None:
    r = _req(title="戦国の寺", narrations=("信長が寺を焼いた秘密。",) * 3)
    from domain.music.lexicon import LEXICON

    keys = {e.key for e in LEXICON}
    assert set(r.matched_terms) <= keys
    assert not any("信長" in t for t in r.matched_terms)


def test_brand_avoided_mood_is_not_chosen_and_preferred_fills_in_when_nothing_matches() -> None:
    script = make_script(title="関ヶ原の合戦")
    avoid = BrandMusicPolicy(brand_id="b", avoided_moods=("tense",), preferred_moods=("solemn",))
    r = derive_requirements(script, make_storyboard(script), fmt="shorts", brand=avoid)
    assert r.mood != "tense"
    plain = make_script()
    r2 = derive_requirements(plain, make_storyboard(plain), fmt="shorts", brand=avoid)
    assert r2.mood == "solemn" and r2.brand_id == "b"


def test_density_and_scene_cut_rate_come_from_numbers() -> None:
    sparse = _req(narrations=("あ。",) * 3)
    dense = _req(narrations=("あ" * 60,) * 3)
    assert sparse.narration_density == "sparse" and dense.narration_density == "dense"
    script = make_script()
    fast = derive_requirements(
        script, make_storyboard(script, per_scene=4), fmt="shorts", brand=BRAND
    )
    assert fast.scene_changes_per_minute == 12 * 60_000 // 24_000
    assert fast.energy == "high"


def test_requirements_are_deterministic_and_have_a_stable_hash() -> None:
    assert _req(title="戦国の寺") == _req(title="戦国の寺")
    assert requirements_sha256(_req()) == requirements_sha256(_req())
    assert requirements_sha256(_req(title="戦国")) != requirements_sha256(_req())


# ------------------------------------------------------------------ scoring


def _entries():
    return [
        entry_for(
            loud_track("bgm-calm", "a", mood_tags=("calm", "japanese"), energy="low", bpm=70)
        ),
        entry_for(loud_track("bgm-tense", "b", mood_tags=("tense",), energy="high", bpm=140)),
        entry_for(loud_track("bgm-mid", "c", mood_tags=("uplifting",), energy="medium", bpm=100)),
    ]


def test_scores_have_per_item_breakdown_and_a_total_that_is_their_sum() -> None:
    req = _req(title="寺の茶")
    ranked = score_candidates(req, _entries(), {}, NOW)
    assert ranked[0].track_id == "bgm-calm"
    for b in ranked:
        assert b.total == sum(p for _, p in b.items)
        assert [i for i, _ in b.items] == list(ScoreItem)


def test_ties_break_by_track_id_and_input_order_never_matters() -> None:
    req = _req()
    entries = [entry_for(loud_track(f"bgm-{c}", c)) for c in "dcba"]
    first = score_candidates(req, entries, {}, NOW)
    assert [b.track_id for b in first] == ["bgm-a", "bgm-b", "bgm-c", "bgm-d"]
    for seed in range(5):
        shuffled = list(entries)
        random.Random(seed).shuffle(shuffled)
        assert score_candidates(req, shuffled, {}, NOW) == first


def test_quarantined_unverified_expired_and_wrong_format_tracks_are_never_scored() -> None:
    req = _req()
    entries = [
        entry_for(
            loud_track(
                "bgm-q",
                "a",
                status="quarantined",
                license_status="unverified",
                license_evidence_sha256=None,
                license_checked_at=None,
            )
        ),
        entry_for(loud_track("bgm-x", "b", valid_until=NOW - timedelta(seconds=1))),
        entry_for(loud_track("bgm-l", "c", allowed_formats=("longform",))),
        entry_for(loud_track("bgm-ok", "d")),
    ]
    assert [b.track_id for b in score_candidates(req, entries, {}, NOW)] == ["bgm-ok"]


def test_policy_approval_with_a_stale_rule_version_is_not_scored_but_owner_approval_is() -> None:
    req = _req()
    base = {"license_approver_kind": "policy"}
    current = entry_for(
        loud_track("bgm-now", "a", license_policy_version=BGM_LICENSE_POLICY_VERSION, **base)
    )
    stale = entry_for(loud_track("bgm-old", "b", license_policy_version="commons-cc0-0", **base))
    owner = entry_for(loud_track("bgm-own", "c", license_approver_kind="owner"))
    ids = {b.track_id for b in score_candidates(req, [current, stale, owner], {}, NOW)}
    assert ids == {"bgm-now", "bgm-own"}


def test_brand_allowlist_and_unmeasured_loudness_exclude_a_track() -> None:
    req = _req()
    entries = [
        entry_for(loud_track("bgm-other", "a", allowed_brands=("someone-else",))),
        entry_for(loud_track("bgm-mine", "b", allowed_brands=(BRAND.brand_id,))),
        entry_for(loud_track("bgm-noloud", "c", loudness_lufs=None)),
    ]
    assert [b.track_id for b in score_candidates(req, entries, {}, NOW)] == ["bgm-mine"]


def test_recent_use_and_content_id_risk_lower_the_score() -> None:
    req = _req()
    e = [
        entry_for(loud_track("bgm-a", "a", content_id_risk="low")),
        entry_for(loud_track("bgm-b", "b", content_id_risk="elevated")),
    ]
    fresh = {b.track_id: b.total for b in score_candidates(req, e, {}, NOW)}
    used = {b.track_id: b.total for b in score_candidates(req, e, {"bgm-a": 3}, NOW)}
    assert fresh["bgm-a"] > fresh["bgm-b"]
    assert used["bgm-a"] < fresh["bgm-a"]


# ------------------------------------------------------------------ decision


def test_a_decision_picks_the_top_track_and_explains_with_at_most_five_ranked() -> None:
    req = _req(title="寺の茶")
    entries = [entry_for(loud_track(f"bgm-{c}", c, mood_tags=("calm",))) for c in "abcdef0"]
    d = select_for_episode(EPISODE, req, entries, {}, NOW, brand=BRAND)
    assert d.track_id == d.ranked[0].track_id and len(d.ranked) == 5
    assert d.requirements_sha256 == requirements_sha256(req)
    assert not d.acquisition_requested and d.cue_sheet is None


def test_no_approved_track_requests_acquisition_and_gives_no_bgm() -> None:
    d = select_for_episode(EPISODE, _req(), [], {}, NOW, brand=BRAND)
    assert d.track_id is None and d.acquisition_requested
    assert d.fallback[0].reason is FallbackReason.NO_APPROVED_TRACK


def test_a_best_score_below_the_minimum_is_no_bgm_and_requests_acquisition() -> None:
    req = _req(title="関ヶ原の合戦")  # tense / high
    poor = entry_for(
        loud_track(
            "bgm-poor",
            "a",
            mood_tags=("serene",),
            energy="low",
            bpm=60,
            duration_ms=1_000,
            content_id_risk="elevated",
        )
    )
    d = select_for_episode(EPISODE, req, [poor], {"bgm-poor": 9}, NOW, brand=BRAND)
    assert d.ranked[0].total < MIN_SELECTION_SCORE
    assert d.track_id is None and d.acquisition_requested
    assert d.fallback[0].reason is FallbackReason.BELOW_MIN_SCORE


def test_a_brand_that_disables_music_never_selects_and_never_requests_acquisition() -> None:
    off = BrandMusicPolicy(brand_id="quiet", music_enabled=False)
    script = make_script()
    req = derive_requirements(script, make_storyboard(script), fmt="shorts", brand=off)
    d = select_for_episode(EPISODE, req, _entries(), {}, NOW, brand=off)
    assert d.track_id is None and not d.acquisition_requested
    assert d.fallback[0].reason is FallbackReason.BRAND_MUSIC_DISABLED


def test_the_same_episode_rerun_and_a_shuffled_catalog_give_the_same_decision() -> None:
    req = _req(title="寺の茶")
    entries = _entries() + [
        entry_for(loud_track(f"bgm-z{c}", c, mood_tags=("calm",))) for c in "def0"
    ]
    first = select_for_episode(EPISODE, req, entries, {}, NOW, brand=BRAND)
    for seed in range(5):
        shuffled = list(entries)
        random.Random(seed).shuffle(shuffled)
        assert select_for_episode(EPISODE, req, shuffled, {}, NOW, brand=BRAND) == first
    assert select_for_episode(EPISODE, req, entries, {}, NOW, brand=BRAND) == first


def test_ladder_candidates_are_the_top_tracks_above_the_minimum_score() -> None:
    req = _req(title="寺の茶")
    d = select_for_episode(EPISODE, req, _entries(), {}, NOW, brand=BRAND)
    assert ladder_candidates(d, limit=2)[0] == d.track_id
    assert len(ladder_candidates(d, limit=2)) <= 2
    none = select_for_episode(EPISODE, req, [], {}, NOW, brand=BRAND)
    assert ladder_candidates(none, limit=2) == ()


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        score_candidates(_req(), [], {}, NOW.replace(tzinfo=None))
