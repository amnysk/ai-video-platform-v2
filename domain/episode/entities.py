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
