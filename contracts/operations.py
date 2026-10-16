"""運用スイッチと日次 Episode 枠の語彙（ADR-0021）。

ここが唯一の宣言元。DB の CHECK もここから導出する（AGENTS.md §8）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class OperationalSwitch(StrEnum):
    """DB に置く停止スイッチ（failure-policy §7）。行が無ければ off。"""

    #: 新しい Episode の自動生成を止める
    PAUSED = "paused"
    #: YouTube への投稿だけを止める（env ``UPLOADS_PAUSED`` との OR）
    UPLOADS_PAUSED = "uploads_paused"


class ClaimOutcome(StrEnum):
    """``DailyEpisodeSlotRepository.claim`` の結果。"""

    #: 新しい枠と planned の Episode を作った
    CREATED = "created"
    #: 同じ trigger_id の枠が既にある（Activity の再試行）
    EXISTING = "existing"
    #: 上限に達しているが、その日の枠に未着手（planned）の Episode があるので再開する
    RESUME = "resume"
    #: 上限に達していて再開すべき Episode も無い
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class DailySlotClaim:
    outcome: ClaimOutcome
    slot_date: date
    episode_id: str | None = None
    slot_index: int | None = None


@dataclass(frozen=True, slots=True)
class DailyEpisodeSlot:
    slot_date: date
    slot_index: int
    trigger_id: str
    episode_id: str
