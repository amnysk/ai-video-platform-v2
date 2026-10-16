"""採点（ADR-0025）。5 成分は 0..1、重みは ``PlannerPolicy.weights``。純粋・決定論。"""

from __future__ import annotations

from contracts.topic_planning import (
    DEFAULT_PLANNER_POLICY,
    FeaturePerformance,
    MemoryItem,
    PlannerPolicy,
    ScoreWeights,
    StrategyProfile,
    TopicCandidate,
)

from .analytics import FEATURE_KINDS

ANALYTICS_FIT = "analytics_fit"
US_YOUNG_FIT = "us_young_fit"
NOVELTY = "novelty"
PORTFOLIO_BALANCE = "portfolio_balance"
PRODUCTION_FIT = "production_fit"
COMPONENTS: tuple[str, ...] = (
    ANALYTICS_FIT,
    US_YOUNG_FIT,
    NOVELTY,
    PORTFOLIO_BALANCE,
    PRODUCTION_FIT,
)

#: 特徴が無いとき・チャンネル平均のときの analytics_fit
NEUTRAL = 0.5


def effective_weights(weights: ScoreWeights, confidence: float) -> dict[str, float]:
    """analytics の重みを confidence 倍し、外した分を他の重みへ比例配分する（合計は保たれる）。"""
    base = {name: float(getattr(weights, name)) for name in COMPONENTS}
    c = min(1.0, max(0.0, confidence))
    removed = base[ANALYTICS_FIT] * (1.0 - c)
    others = sum(v for k, v in base.items() if k != ANALYTICS_FIT)
    out = {ANALYTICS_FIT: base[ANALYTICS_FIT] * c}
    for k, v in base.items():
        if k != ANALYTICS_FIT:
            out[k] = v + (removed * v / others if others else 0.0)
    return out


def analytics_fit(candidate: TopicCandidate, features: list[FeaturePerformance]) -> float:
    """一致する特徴の相対成績の平均 r を r/(1+r) で 0..1 へ（平均 = 0.5）。"""
    by_name = {f.feature: f.relative_performance for f in features}
    values = (candidate.theme, candidate.angle.value, candidate.era, candidate.subject)
    matched = [
        by_name[name]
        for name in (f"{k}:{v}" for k, v in zip(FEATURE_KINDS, values, strict=True))
        if name in by_name
    ]
    if not matched:
        return NEUTRAL
    r = max(0.0, sum(matched) / len(matched))
    return r / (1.0 + r)


def us_young_fit(
    candidate: TopicCandidate,
    strategy: StrategyProfile,
    policy: PlannerPolicy = DEFAULT_PLANNER_POLICY,
) -> float:
    preferred = candidate.angle in strategy.preferred_angles
    share = policy.preferred_angle_share
    fit = share * preferred + (1.0 - share) * candidate.audience_fit
    if candidate.theme not in strategy.content_pillars:
        fit *= policy.off_pillar_factor
    return fit


def recent_window(memory: list[MemoryItem], policy: PlannerPolicy) -> list[MemoryItem]:
    """day の新しい順に ``portfolio_window`` 件（同日は元の順）。"""
    ordered = sorted(enumerate(memory), key=lambda im: (im[1].day, -im[0]), reverse=True)
    return [m for _, m in ordered[: policy.portfolio_window]]


def portfolio_balance(
    candidate: TopicCandidate,
    recent: list[MemoryItem],
    strategy: StrategyProfile,
    policy: PlannerPolicy = DEFAULT_PLANNER_POLICY,
) -> float:
    """柱の目標比より少ない theme ほど高い。直近と同じ era / angle は下げる。"""
    target = strategy.content_pillars.get(candidate.theme)
    if not target:
        return 0.0
    themed = [m for m in recent if m.theme is not None]
    share = sum(m.theme == candidate.theme for m in themed) / len(themed) if themed else 0.0
    score = min(1.0, max(0.0, 1.0 - share / (2.0 * target)))
    if recent:
        last = recent[0]
        if last.era == candidate.era:
            score *= policy.recent_repeat_factor
        if last.angle == candidate.angle.value:
            score *= policy.recent_repeat_factor
    return score


def score_candidate(
    candidate: TopicCandidate,
    *,
    duplicate_score: float,
    penalty: float,
    confidence: float,
    features: list[FeaturePerformance],
    recent: list[MemoryItem],
    strategy: StrategyProfile,
    policy: PlannerPolicy,
) -> tuple[float, dict[str, float]]:
    """(final, breakdown)。final = Σ w_i s_i − penalty（0 未満にしない）。"""
    components = {
        ANALYTICS_FIT: analytics_fit(candidate, features),
        US_YOUNG_FIT: us_young_fit(candidate, strategy, policy),
        NOVELTY: 1.0 - duplicate_score,
        PORTFOLIO_BALANCE: portfolio_balance(candidate, recent, strategy, policy),
        PRODUCTION_FIT: candidate.visual_fit,
    }
    weights = effective_weights(policy.weights, confidence)
    raw = sum(weights[k] * components[k] for k in COMPONENTS)
    final = max(0.0, raw - penalty)
    breakdown = {
        **components,
        **{f"weight_{k}": weights[k] for k in COMPONENTS},
        "confidence": confidence,
        "penalty": penalty,
        "raw": raw,
    }
    return final, breakdown
