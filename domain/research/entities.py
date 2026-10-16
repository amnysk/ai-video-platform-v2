"""Research のドメイン表現（ADR-0037）。DB の行から作る読み取り専用の値。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from contracts.research import (
    ResearchArtifactType,
    ResearchCall,
    ResearchCallStatus,
    ResearchKind,
    ResearchStatus,
)


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """調査依頼の 1 行。``payload`` / ``limits`` は依頼時に凍結した正準 JSON。"""

    id: str
    idempotency_key: str
    request_hash: str
    kind: ResearchKind
    status: ResearchStatus
    requester: str
    channel_id: str
    #: 参照であって所有ではない（Evidence は複数 Episode が共有しうる）
    episode_id: str | None
    payload: dict[str, Any]
    limits: dict[str, Any]
    policy_version: str
    schema_version: str
    provider_config_version: str
    as_of: datetime
    created_at: datetime
    updated_at: datetime
    blocked_reason: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResearchCallRecord:
    """外部呼び出し台帳の 1 行（INV-36）。"""

    id: str
    request_id: str
    call: ResearchCall
    call_seq: int
    idempotency_key: str
    input_hash: str
    #: どの Adapter が呼ばれたか（診断用ラベル。枠の単位ではない）
    provider: str
    status: ResearchCallStatus
    reserved_at: datetime
    estimated_cost_usd: Decimal | None = None
    quota_units: int | None = None
    dispatched_at: datetime | None = None
    settled_at: datetime | None = None
    error_summary: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchArtifactRecord:
    """research 所有の成果物の 1 世代。"""

    id: str
    request_id: str
    artifact_type: ResearchArtifactType
    schema_version: str
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int
    version: int
    created_at: datetime
    superseded_at: datetime | None = None


__all__ = ["ResearchArtifactRecord", "ResearchCallRecord", "ResearchRequest"]
