"""重複判定（ADR-0025 / INV-24）。純粋・決定論。

- Level 1 EXACT: 同じ subject かつ同じ angle、または正規化タイトルの語集合が一致
- Level 2 SEMANTIC: 類似度 >= ``policy.similarity_reject``
- Level 3 SAME_SUBJECT: 同じ subject・別 angle。cooldown 内は reject、外は penalty
- それ以外: 類似度の帯（strong / mild）に応じた penalty

Content Memory の status は見ない（予定・制作中・失敗も「使った Topic」に数える）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from contracts.topic_planning import (
    DEFAULT_PLANNER_POLICY,
    HARD_DUPLICATE_LEVELS,
    DuplicateLevel,
    MemoryItem,
    PlannerPolicy,
    TopicCandidate,
)

from .normalize import jaccard, normalize_title

#: 重さの順（大きいほど深刻）
_SEVERITY: dict[DuplicateLevel, int] = {
    DuplicateLevel.NONE: 0,
    DuplicateLevel.SAME_SUBJECT: 1,
    DuplicateLevel.SEMANTIC: 2,
    DuplicateLevel.EXACT: 3,
}


@dataclass(frozen=True, slots=True)
class DuplicateResult:
    level: DuplicateLevel
    #: Content Memory に対する最大の類似度（0..1）
    score: float
    #: 判定を決めた Memory の topic（無ければ None）
    matched_topic: str | None
    rejected: bool
    penalty: float
    reason: str


def _subject_matches(c: TopicCandidate, m: MemoryItem) -> bool:
    if m.subject is None:
        return False
    return c.subject == m.subject or c.subject in m.entities or m.subject in c.entities


def similarity(
    c: TopicCandidate, m: MemoryItem, policy: PlannerPolicy = DEFAULT_PLANNER_POLICY
) -> float:
    """0..1。特徴の加重類似度と正規化タイトルの Jaccard の大きい方。"""
    title = jaccard(normalize_title(c.topic), normalize_title(m.topic))
    if m.subject is None:
        return title
    w = policy.feature_similarity_weights
    feature = (
        w["subject"] * _subject_matches(c, m)
        + w["entities"] * jaccard(set(c.entities), set(m.entities))
        + w["theme"] * (c.theme == m.theme)
        + w["era"] * (c.era == m.era)
        + w["angle"] * (c.angle.value == m.angle)
    )
    return max(feature, title)


def _band_penalty(score: float, policy: PlannerPolicy) -> float:
    if score >= policy.similarity_strong_penalty:
        return policy.strong_penalty
    if score >= policy.similarity_mild_penalty:
        return policy.mild_penalty
    return 0.0


def _classify_one(
    c: TopicCandidate, m: MemoryItem, policy: PlannerPolicy, today: date
) -> DuplicateResult:
    score = similarity(c, m, policy)
    same_subject = m.subject is not None and c.subject == m.subject
    if (same_subject and c.angle.value == m.angle) or normalize_title(c.topic) == normalize_title(
        m.topic
    ):
        return DuplicateResult(
            DuplicateLevel.EXACT, score, m.topic, True, 0.0, f"exact duplicate of {m.topic!r}"
        )
    if score >= policy.similarity_reject:
        return DuplicateResult(
            DuplicateLevel.SEMANTIC,
            score,
            m.topic,
            True,
            0.0,
            f"semantic duplicate of {m.topic!r} (similarity {score:.2f})",
        )
    band = _band_penalty(score, policy)
    if same_subject:
        age = abs((today - date.fromisoformat(m.day)).days)
        if age <= policy.same_subject_cooldown_days:
            return DuplicateResult(
                DuplicateLevel.SAME_SUBJECT,
                score,
                m.topic,
                True,
                0.0,
                f"same subject as {m.topic!r} within cooldown ({age} days)",
            )
        return DuplicateResult(
            DuplicateLevel.SAME_SUBJECT,
            score,
            m.topic,
            False,
            max(band, policy.same_subject_penalty),
            f"same subject as {m.topic!r} ({age} days ago)",
        )
    return DuplicateResult(
        DuplicateLevel.NONE,
        score,
        m.topic if band else None,
        False,
        band,
        f"similar to {m.topic!r} (similarity {score:.2f})" if band else "",
    )


def classify_duplicate(
    candidate: TopicCandidate,
    memory: list[MemoryItem],
    policy: PlannerPolicy,
    today: date,
) -> DuplicateResult:
    """全 Memory と比べ、最も深刻な判定を返す（同順位は Memory の先頭側）。"""
    decisive = DuplicateResult(DuplicateLevel.NONE, 0.0, None, False, 0.0, "")
    top_score = 0.0
    top_penalty = 0.0
    for m in memory:
        r = _classify_one(candidate, m, policy, today)
        top_score = max(top_score, r.score)
        top_penalty = max(top_penalty, r.penalty)
        key = (r.rejected, _SEVERITY[r.level], r.penalty, r.score)
        best = (decisive.rejected, _SEVERITY[decisive.level], decisive.penalty, decisive.score)
        if key > best:
            decisive = r
    return DuplicateResult(
        decisive.level,
        top_score,
        decisive.matched_topic,
        decisive.rejected,
        0.0 if decisive.rejected else top_penalty,
        decisive.reason,
    )


def is_hard(result: DuplicateResult) -> bool:
    return result.level in HARD_DUPLICATE_LEVELS


def candidate_as_memory(candidate: TopicCandidate, day: date) -> MemoryItem:
    """同じ batch 内の重複判定のため、先の候補を Memory として扱う。"""
    return MemoryItem(
        topic=candidate.topic,
        subject=candidate.subject,
        entities=list(candidate.entities),
        era=candidate.era,
        theme=candidate.theme,
        angle=candidate.angle.value,
        day=day.isoformat(),
        status="candidate",
    )
