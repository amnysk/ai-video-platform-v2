"""Jobのドメイン表現。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from contracts.states import FailureClass, JobStatus, JobType


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    episode_id: str
    type: JobType
    status: JobStatus
    attempts: int
    max_attempts: int
    failure_class: FailureClass | None
    created_at: datetime
    updated_at: datetime
