"""Episode 1 件の選曲の決定を作る（ADR-0087 §5 / §7、INV-153）。純粋関数。

要件 → 点数 → 上位（最大 ``RANKED_LIMIT``）→ 最上位が ``MIN_SELECTION_SCORE`` 以上なら選ぶ。
届かなければ BGM 無し（収集の需要を積む）。cue sheet はここでは作らない（配置は
``plan_cue_sheet``。描画側が RenderPlan を持っている）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from contracts.music_selection import (
    BGM_SELECTION_RULE_VERSION,
    MIN_SELECTION_SCORE,
    BgmSelectionDecision,
    BrandMusicPolicy,
    FallbackEvent,
    FallbackReason,
    FallbackStep,
    MusicRequirements,
)
from domain.music.ports import CatalogEntry
from domain.music.requirements import requirements_sha256
from domain.music.scoring import score_candidates

RANKED_LIMIT = 5


def select_for_episode(
    episode_id: str,
    requirements: MusicRequirements,
    entries: Iterable[CatalogEntry],
    recent_usage: Mapping[str, int],
    now: datetime,
    *,
    brand: BrandMusicPolicy,
) -> BgmSelectionDecision:
    digest = requirements_sha256(requirements)

    def _decision(**extra: object) -> BgmSelectionDecision:
        return BgmSelectionDecision.model_validate(
            {
                "episode_id": episode_id,
                "rule_version": BGM_SELECTION_RULE_VERSION,
                "requirements": requirements,
                "requirements_sha256": digest,
                **extra,
            }
        )

    if not brand.music_enabled:
        return _decision(
            fallback=(
                FallbackEvent(step=FallbackStep.NO_BGM, reason=FallbackReason.BRAND_MUSIC_DISABLED),
            )
        )

    ranked = score_candidates(
        requirements, entries, recent_usage, now, avoided_moods=brand.avoided_moods
    )[:RANKED_LIMIT]
    if not ranked:
        return _decision(
            fallback=(
                FallbackEvent(step=FallbackStep.NO_BGM, reason=FallbackReason.NO_APPROVED_TRACK),
            ),
            acquisition_requested=True,
        )
    best = ranked[0]
    if best.total < MIN_SELECTION_SCORE:
        return _decision(
            ranked=ranked,
            fallback=(
                FallbackEvent(step=FallbackStep.NO_BGM, reason=FallbackReason.BELOW_MIN_SCORE),
            ),
            acquisition_requested=True,
        )
    return _decision(
        ranked=ranked,
        track_id=best.track_id,
        fallback=(FallbackEvent(step=FallbackStep.SELECTED, track_id=best.track_id),),
    )


def ladder_candidates(decision: BgmSelectionDecision, *, limit: int) -> tuple[str, ...]:
    """梯子で試す曲（最上位から、``MIN_SELECTION_SCORE`` 以上のものだけ、最大 ``limit`` 曲）。"""
    if decision.track_id is None:
        return ()
    ids = [b.track_id for b in decision.ranked if b.total >= MIN_SELECTION_SCORE]
    return tuple(ids[:limit])


__all__ = ["RANKED_LIMIT", "ladder_candidates", "select_for_episode"]
