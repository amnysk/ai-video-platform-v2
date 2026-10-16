"""候補の重複判定・採点・選択（ADR-0025 / INV-24）。純粋・決定論。

- Content Memory と、同じ batch の先の候補（ordinal の小さい方を残す）に対して重複判定
- hard duplicate（L1 / L2）と cooldown 内の L3 は採点しても選ばない
- 最高点を選ぶ。同点は ordinal の小さい方。選べなければ ``chosen_index`` は None

Content profile（Shorts / Long）では分岐しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from contracts.topic_planning import (
    STRATEGY_PROFILES,
    DuplicateLevel,
    PlannerPolicy,
    PlanningContext,
    StrategyProfile,
    TopicCandidate,
)

from .duplicates import candidate_as_memory, classify_duplicate
from .scoring import recent_window, score_candidate


@dataclass(frozen=True, slots=True)
class Evaluation:
    ordinal: int
    candidate: TopicCandidate
    level: DuplicateLevel
    duplicate_score: float
    duplicate_of: str | None
    penalty: float
    breakdown: dict[str, float] = field(hash=False)
    score: float
    rejected: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SelectionResult:
    evaluations: list[Evaluation]
    chosen_index: int | None

    @property
    def chosen(self) -> Evaluation | None:
        return None if self.chosen_index is None else self.evaluations[self.chosen_index]

    @property
    def rejected_subjects(self) -> list[str]:
        """reject された候補の subject（重複なし・出現順）。次 round の avoid_subjects 用。"""
        seen: dict[str, None] = {}
        for e in self.evaluations:
            if e.rejected:
                seen.setdefault(e.candidate.subject, None)
        return list(seen)


def select(
    candidates: list[TopicCandidate],
    context: PlanningContext,
    policy: PlannerPolicy,
    today: date,
    strategy: StrategyProfile | None = None,
) -> SelectionResult:
    """``strategy`` を省くと ``context.request.strategy_profile_id`` の profile を使う。"""
    profile = strategy or STRATEGY_PROFILES[context.request.strategy_profile_id]
    memory = context.memory
    recent = recent_window(memory, policy)
    confidence = context.analytics.confidence
    features = context.analytics.features

    evaluations: list[Evaluation] = []
    for ordinal, c in enumerate(candidates):
        vs_memory = classify_duplicate(c, memory, policy, today)
        earlier = [candidate_as_memory(p, today) for p in candidates[:ordinal]]
        vs_batch = classify_duplicate(c, earlier, policy, today)

        decisive = vs_memory
        rejected = vs_memory.rejected
        reason = vs_memory.reason
        if not rejected and vs_batch.rejected:
            decisive = vs_batch
            rejected = True
            reason = f"in-batch: {vs_batch.reason}"
        penalty = 0.0 if rejected else vs_memory.penalty
        final, breakdown = score_candidate(
            c,
            duplicate_score=vs_memory.score,
            penalty=penalty,
            confidence=confidence,
            features=features,
            recent=recent,
            strategy=profile,
            policy=policy,
        )
        evaluations.append(
            Evaluation(
                ordinal=ordinal,
                candidate=c,
                level=decisive.level,
                duplicate_score=decisive.score,
                duplicate_of=decisive.matched_topic,
                penalty=penalty,
                breakdown=breakdown,
                score=final,
                rejected=rejected,
                reason=reason,
            )
        )

    chosen: int | None = None
    for e in evaluations:
        if e.rejected:
            continue
        if chosen is None or e.score > evaluations[chosen].score:
            chosen = e.ordinal
    return SelectionResult(evaluations=evaluations, chosen_index=chosen)
