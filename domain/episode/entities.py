"""Episodeのドメイン表現。I/Oに依存しない読み取り用の値。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from contracts.states import EpisodeStatus


@dataclass(frozen=True, slots=True)
class Episode:
    id: str
    status: EpisodeStatus
    topic: str | None
    created_at: datetime
    updated_at: datetime
    #: 題材を決めた TopicPlan（ADR-0025）。Planner 導入前の Episode は None
    topic_plan_id: str | None = None
