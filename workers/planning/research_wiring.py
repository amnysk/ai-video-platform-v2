"""planning worker の Research への opt-in 接続の組み立て（ADR-0038 §B6 / ADR-0039 §B6）。

``run_worker`` は**どちらかの設定が ON のときだけ**このモジュールを import する（OFF の worker は
Research のコードを読み込まない）。組み立ての唯一の場所:

- ``PLANNER_TREND_ENABLED`` → ``GatewayTrendSource``（Topic Planner が確定済みの Trend を読む）
- ``SCRIPT_EVIDENCE_ENABLED`` → ``ScriptEvidenceActivities`` と ``EvidenceScriptWorkflow``

どちらも ``YOUTUBE_CHANNEL_ID`` が無ければ組まない（警告して OFF と同じ動き）。
Research の依頼・Trend は channel id で束ねるので、推測した値で読まない・依頼しない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from infrastructure.config import Settings
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from infrastructure.research.registry import build_claim_extractor
from infrastructure.research.verification import ScriptVerifier
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.temporal.research_starter import (
    ResearchWorkflowStarter,
    TemporalResearchStarter,
)
from workers.planning.script_evidence_activities import ScriptEvidenceActivities
from workers.planning.topic_trend import GatewayTrendSource

__all__ = ["ResearchLinks", "build_research_links"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchLinks:
    trend: GatewayTrendSource | None = None
    evidence: ScriptEvidenceActivities | None = None


def build_research_links(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    client: Client | None,
    clock: Callable[[], datetime],
    starter: ResearchWorkflowStarter | None = None,
) -> ResearchLinks:
    """設定から接続を組む。``starter`` はテストが差し替える（既定は ``client`` で起動する）。"""
    if not (settings.planner_trend_enabled or settings.script_evidence_enabled):
        return ResearchLinks()
    channel_id = settings.youtube_channel_id
    if not channel_id:
        logger.warning(
            "PLANNER_TREND_ENABLED / SCRIPT_EVIDENCE_ENABLED need YOUTUBE_CHANNEL_ID; "
            "research links stay off"
        )
        return ResearchLinks()
    gateway = ResearchGateway(
        session_factory=session_factory,
        store=store,
        config=GatewayConfig.from_settings(settings),
        clock=clock,
    )
    trend = (
        GatewayTrendSource(
            reader=gateway,
            channel_id=channel_id,
            fresh_hours=settings.trend_fresh_hours,
            clock=clock,
        )
        if settings.planner_trend_enabled
        else None
    )
    evidence = None
    if settings.script_evidence_enabled:
        if starter is None:
            if client is None:
                raise ValueError("SCRIPT_EVIDENCE_ENABLED needs a Temporal client")
            starter = TemporalResearchStarter(client)
        extractor = build_claim_extractor(settings)
        evidence = ScriptEvidenceActivities(
            session_factory=session_factory,
            store=store,
            gateway=gateway,
            verifier=ScriptVerifier(
                session_factory=session_factory,
                store=store,
                bucket=settings.minio_bucket,
                extractor=extractor,
            ),
            starter=starter,
            channel_id=channel_id,
            default_strategy_profile_id=settings.topic_strategy_profile_id,
            extractor=extractor,
            clock=clock,
        )
    return ResearchLinks(trend=trend, evidence=evidence)
