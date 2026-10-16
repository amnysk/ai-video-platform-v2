"""Topic Planner の純粋ドメイン（ADR-0025）: 正規化・重複判定・analytics・採点・選択。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from contracts.states import FailureClass
from contracts.topic_planning import (
    DEFAULT_PLANNER_POLICY,
    STRATEGY_PROFILES,
    AnalyticsMode,
    AnalyticsSummary,
    DuplicateLevel,
    FeaturePerformance,
    MemoryItem,
    PlanningContext,
    TopicAngle,
    TopicCandidate,
    TopicPlannerInput,
)
from domain import errors
from domain.errors import NON_RETRYABLE_ERROR_TYPE_NAMES, classify_failure
from domain.topic_planning import (
    AnalyticsReport,
    AudienceShares,
    VideoMetrics,
    analytics_confidence,
    classify_duplicate,
    derive_feature_performance,
    effective_weights,
    normalize_title,
    report_confidence,
    select,
    similarity,
)

POLICY = DEFAULT_PLANNER_POLICY
STRATEGY = STRATEGY_PROFILES["us_young_history_v1"]
TODAY = date(2026, 9, 18)


def cand(**kw: object) -> TopicCandidate:
    base: dict[str, object] = {
        "topic": "Why Samurai Committed Seppuku",
        "subject": "seppuku",
        "entities": ["samurai", "bushido"],
        "era": "edo",
        "theme": "ritual_and_religion",
        "angle": TopicAngle.REASON,
        "hook": "It was not about honor alone",
        "visual_concept": "a quiet courtyard at dawn",
        "reason": "surprising and concrete",
        "audience_fit": 0.7,
        "visual_fit": 0.7,
    }
    base.update(kw)
    return TopicCandidate.model_validate(base)


def mem(days_ago: int = 5, **kw: object) -> MemoryItem:
    base: dict[str, object] = {
        "topic": "Why Samurai Committed Seppuku",
        "subject": "seppuku",
        "entities": ["samurai", "bushido"],
        "era": "edo",
        "theme": "ritual_and_religion",
        "angle": "reason",
        "day": (TODAY - timedelta(days=days_ago)).isoformat(),
        "status": "uploaded",
    }
    base.update(kw)
    return MemoryItem(**base)  # type: ignore[arg-type]


def legacy(title: str, days_ago: int = 100) -> MemoryItem:
    return MemoryItem(
        topic=title,
        subject=None,
        entities=[],
        era=None,
        theme=None,
        angle=None,
        day=(TODAY - timedelta(days=days_ago)).isoformat(),
        status="completed",
    )


def context(
    memory: list[MemoryItem] | None = None,
    *,
    confidence: float = 0.0,
    features: list[FeaturePerformance] | None = None,
    content_profile_id: str = "shorts",
) -> PlanningContext:
    return PlanningContext(
        request=TopicPlannerInput(
            plan_date=TODAY.isoformat(),
            strategy_profile_id=STRATEGY.strategy_id,
            content_profile_id=content_profile_id,
        ),
        analytics=AnalyticsSummary(
            mode=AnalyticsMode.NORMAL if confidence else AnalyticsMode.NO_ANALYTICS,
            confidence=confidence,
            features=features or [],
        ),
        memory=memory or [],
    )


# ------------------------------------------------------------------------ normalize


def test_normalize_title_is_case_punctuation_stopword_and_plural_insensitive() -> None:
    assert normalize_title("Why Samurai Committed Seppuku?") == normalize_title(
        "why the samurai committed SEPPUKU"
    )
    assert normalize_title("The Castles of Japan") == normalize_title("japan castle")


# ------------------------------------------------------------------------ duplicates


def test_seppuku_rephrased_title_is_a_hard_duplicate() -> None:
    c = cand(topic="Why Japanese Warriors Cut Open Their Stomachs", entities=["bushi"])
    d = classify_duplicate(c, [mem()], POLICY, TODAY)
    assert d.level in {DuplicateLevel.EXACT, DuplicateLevel.SEMANTIC}
    assert d.rejected
    assert d.matched_topic == "Why Samurai Committed Seppuku"


def test_exact_duplicate_of_published_item() -> None:
    d = classify_duplicate(cand(), [mem(days_ago=400, status="uploaded")], POLICY, TODAY)
    assert d.level is DuplicateLevel.EXACT
    assert d.rejected


@pytest.mark.parametrize("status", ["planned", "in_progress", "failed", "assigned"])
def test_duplicate_of_unpublished_item_counts_regardless_of_status(status: str) -> None:
    d = classify_duplicate(cand(), [mem(days_ago=-1, status=status)], POLICY, TODAY)
    assert d.level is DuplicateLevel.EXACT
    assert d.rejected


def _same_subject_other_angle(days_ago: int) -> MemoryItem:
    # 同 subject・別 angle・他の特徴は異なる（Level 2 に達しない）
    return mem(
        days_ago=days_ago,
        topic="The Origins of Ritual Suicide",
        angle="origin",
        entities=["kamakura_shogunate"],
        era="kamakura",
        theme="warriors_and_war",
    )


def test_same_subject_inside_cooldown_is_rejected() -> None:
    days = POLICY.same_subject_cooldown_days - 1
    d = classify_duplicate(cand(), [_same_subject_other_angle(days)], POLICY, TODAY)
    assert d.level is DuplicateLevel.SAME_SUBJECT
    assert d.rejected


def test_same_subject_outside_cooldown_is_penalised_but_allowed() -> None:
    days = POLICY.same_subject_cooldown_days + 1
    d = classify_duplicate(cand(), [_same_subject_other_angle(days)], POLICY, TODAY)
    assert d.level is DuplicateLevel.SAME_SUBJECT
    assert not d.rejected
    assert d.penalty >= POLICY.same_subject_penalty


def test_legacy_title_only_memory_is_compared_by_title() -> None:
    exact = classify_duplicate(cand(), [legacy("why samurai committed seppuku!")], POLICY, TODAY)
    assert exact.level is DuplicateLevel.EXACT and exact.rejected

    near = classify_duplicate(
        cand(), [legacy("The Real Reason Samurai Committed Seppuku")], POLICY, TODAY
    )
    assert near.level is DuplicateLevel.NONE
    assert not near.rejected
    assert near.penalty > 0
    assert 0 < near.score < 1
    assert similarity(cand(), legacy("Bamboo Forests of Kyoto")) == 0


def test_unrelated_memory_is_not_a_duplicate() -> None:
    other = mem(
        topic="What Edo Children Ate",
        subject="edo_food",
        entities=["rice"],
        era="heian",
        theme="daily_life",
        angle="daily_life",
    )
    d = classify_duplicate(cand(), [other], POLICY, TODAY)
    assert d.level is DuplicateLevel.NONE and not d.rejected and d.penalty == 0


def test_intra_batch_duplicates_keep_the_earlier_ordinal() -> None:
    first = cand()
    again = cand(topic="Why Japanese Warriors Cut Open Their Stomachs")
    other = cand(topic="Life of an Edo Firefighter", subject="edo_firefighters", entities=[])
    result = select([first, again, other], context(), POLICY, TODAY)
    ev = result.evaluations
    assert not ev[0].rejected
    assert ev[1].rejected and ev[1].duplicate_of == first.topic
    assert not ev[2].rejected


# ------------------------------------------------------------------------ analytics


def test_zero_confidence_removes_analytics_weight_and_weights_sum_to_one() -> None:
    w = effective_weights(POLICY.weights, 0.0)
    assert w["analytics_fit"] == 0
    assert sum(w.values()) == pytest.approx(1.0)
    half = effective_weights(POLICY.weights, 0.5)
    assert half["analytics_fit"] == pytest.approx(POLICY.weights.analytics_fit / 2)
    assert sum(half.values()) == pytest.approx(1.0)


def test_analytics_confidence_scales_with_data_volume() -> None:
    assert analytics_confidence(0, 0, POLICY) == 0
    assert (
        analytics_confidence(POLICY.confidence_full_views, POLICY.confidence_full_videos, POLICY)
        == 1
    )
    assert analytics_confidence(
        POLICY.confidence_full_views * 10, POLICY.confidence_full_videos // 2, POLICY
    ) == pytest.approx(0.5)


def _video(vid: str, views: float) -> VideoMetrics:
    return VideoMetrics(
        video_id=vid,
        views=views,
        estimated_minutes_watched=views / 2,
        average_view_duration=30,
        average_view_percentage=60,
        likes=0,
        shares=0,
        subscribers_gained=0,
    )


def test_feature_performance_is_relative_to_channel_average() -> None:
    report = AnalyticsReport(
        videos_by_window={28: [_video("a", 300), _video("b", 100), _video("c", 200)]},
        audience=AudienceShares(),
    )
    features = {
        "a": mem(theme="warriors_and_war", angle="reason"),
        "b": mem(theme="daily_life", angle="reason"),
        # c は Plan に紐付かない（legacy）: チャンネル平均には入るが特徴は無い
    }
    perf = {f.feature: f for f in derive_feature_performance(report, features, 28)}
    assert perf["theme:warriors_and_war"].relative_performance == pytest.approx(1.5)
    assert perf["theme:daily_life"].relative_performance == pytest.approx(0.5)
    assert perf["angle:reason"].relative_performance == pytest.approx(1.0)
    assert perf["angle:reason"].videos == 2
    assert report_confidence(report, POLICY, 28) == pytest.approx(
        min(1, 600 / POLICY.confidence_full_views) * min(1, 3 / POLICY.confidence_full_videos)
    )
    assert derive_feature_performance(AnalyticsReport(), features, 28) == []


# ------------------------------------------------------------------------ scoring / selection


def _analytics_favored_pair() -> tuple[TopicCandidate, TopicCandidate, list[FeaturePerformance]]:
    a = cand(
        topic="Samurai Armor Secrets",
        subject="samurai_armor",
        entities=[],
        theme="warriors_and_war",
        angle=TopicAngle.MYSTERY,
        audience_fit=0.4,
    )
    b = cand(
        topic="Edo Bathhouse Etiquette",
        subject="edo_bathhouses",
        entities=[],
        theme="daily_life",
        angle=TopicAngle.MYSTERY,
        audience_fit=0.8,
    )
    feats = [FeaturePerformance("theme:warriors_and_war", 4.0, 10)]
    return a, b, feats


def test_confidence_changes_the_ranking() -> None:
    a, b, feats = _analytics_favored_pair()
    low = select([a, b], context(confidence=0.0, features=feats), POLICY, TODAY)
    high = select([a, b], context(confidence=1.0, features=feats), POLICY, TODAY)
    assert low.chosen_index == 1
    assert high.chosen_index == 0
    assert (
        low.evaluations[0].breakdown["analytics_fit"]
        == high.evaluations[0].breakdown["analytics_fit"]
    )
    assert low.evaluations[0].breakdown["confidence"] == 0.0


def test_saturated_high_performing_theme_loses_to_underrepresented_pillar() -> None:
    memory = [
        mem(
            days_ago=i + 1,
            topic=f"Warrior story {i}",
            subject=f"warrior_{i}",
            entities=[],
            theme="warriors_and_war",
            era="heian",
            angle="person",
        )
        for i in range(POLICY.portfolio_window)
    ]
    repeat = cand(
        topic="Ninja Myths",
        subject="ninja",
        entities=[],
        theme="warriors_and_war",
        angle=TopicAngle.MYTH_VS_FACT,
    )
    fresh = cand(
        topic="Edo Street Food",
        subject="edo_street_food",
        entities=[],
        theme="daily_life",
        angle=TopicAngle.MYTH_VS_FACT,
    )
    feats = [FeaturePerformance("theme:warriors_and_war", 3.0, POLICY.portfolio_window)]
    result = select([repeat, fresh], context(memory, confidence=1.0, features=feats), POLICY, TODAY)
    assert (
        result.evaluations[0].breakdown["analytics_fit"]
        > result.evaluations[1].breakdown["analytics_fit"]
    )
    assert result.chosen_index == 1


def test_hard_duplicate_is_never_selected_even_with_highest_raw_score() -> None:
    dup = cand(audience_fit=1.0, visual_fit=1.0)
    weak = cand(
        topic="Edo Street Food",
        subject="edo_street_food",
        entities=[],
        theme="daily_life",
        angle=TopicAngle.EVENT,
        audience_fit=0.0,
        visual_fit=0.0,
    )
    result = select([dup, weak], context([mem(days_ago=400)]), POLICY, TODAY)
    assert result.evaluations[0].rejected
    assert result.evaluations[0].level is DuplicateLevel.EXACT
    assert result.evaluations[0].reason
    assert result.chosen_index == 1


def test_all_rejected_returns_none() -> None:
    result = select(
        [cand(), cand(topic="Why Samurai Cut Their Stomachs")], context([mem()]), POLICY, TODAY
    )
    assert result.chosen_index is None
    assert all(e.rejected for e in result.evaluations)


def test_tie_breaks_on_lowest_ordinal() -> None:
    a = cand(topic="Edo Street Food", subject="edo_street_food", entities=[], theme="daily_life")
    b = cand(topic="Heian Court Poetry", subject="heian_poetry", entities=[], theme="daily_life")
    result = select([a, b], context(), POLICY, TODAY)
    assert result.evaluations[0].score == pytest.approx(result.evaluations[1].score)
    assert result.chosen_index == 0


def test_selection_is_deterministic_and_breakdown_is_complete() -> None:
    a, b, feats = _analytics_favored_pair()
    ctx = context([mem(days_ago=40)], confidence=0.6, features=feats)
    first = select([a, b, cand()], ctx, POLICY, TODAY)
    second = select([a, b, cand()], ctx, POLICY, TODAY)
    assert first == second
    keys = {
        "analytics_fit",
        "us_young_fit",
        "novelty",
        "portfolio_balance",
        "production_fit",
        "confidence",
        "penalty",
    }
    for e in first.evaluations:
        assert keys <= set(e.breakdown)
        assert e.score >= 0


def test_core_does_not_branch_on_content_profile() -> None:
    a, b, feats = _analytics_favored_pair()
    shorts = select([a, b], context(confidence=0.5, features=feats), POLICY, TODAY)
    long_form = select(
        [a, b],
        context(confidence=0.5, features=feats, content_profile_id="long_form"),
        POLICY,
        TODAY,
    )
    assert shorts.chosen_index is not None
    assert shorts.evaluations == long_form.evaluations
    assert shorts.chosen_index == long_form.chosen_index


def test_policy_values_drive_thresholds() -> None:
    strict = POLICY.model_copy(update={"same_subject_cooldown_days": 1000})
    d = classify_duplicate(cand(), [_same_subject_other_angle(500)], strict, TODAY)
    assert d.rejected
    assert not classify_duplicate(cand(), [_same_subject_other_angle(500)], POLICY, TODAY).rejected


# ------------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (errors.TopicCandidateContractError("x"), FailureClass.RETRYABLE),
        (errors.AnalyticsUnavailableError("x"), FailureClass.TRANSIENT),
        (errors.TopicPlanningExhaustedError("x"), FailureClass.NEEDS_INPUT),
    ],
)
def test_topic_planning_errors_classify_by_their_base(
    exc: Exception, expected: FailureClass
) -> None:
    assert classify_failure(exc) is expected
    assert errors.failure_class_from_type_name(type(exc).__name__) is expected
    non_retryable = expected in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
    assert (type(exc).__name__ in NON_RETRYABLE_ERROR_TYPE_NAMES) is non_retryable
