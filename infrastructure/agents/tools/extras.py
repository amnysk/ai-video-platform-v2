"""tool executor に注入する adapter の組み立て（ADR-0047 §3）。

``JPMAS2_AGENT_TOOLS`` が有効なときだけ呼ぶ。組めない adapter は黙って捏造せず**入れない**
（入れなければ executor が供給されず、gateway が拒否する）。秘密（OAuth token）はログに出さない。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from temporalio.client import Client

from infrastructure.agents.tools import EXTRA_INSIGHT_PROVIDER, EXTRA_RESEARCH_STARTER
from infrastructure.analytics.video_insights import YouTubeVideoInsightProvider
from infrastructure.config import Settings
from infrastructure.temporal.research_starter import TemporalResearchStarter
from infrastructure.youtube.errors import YouTubeAuthError
from infrastructure.youtube.oauth import RefreshTokenCredentials

logger = logging.getLogger(__name__)


def build_tool_extras(
    settings: Settings, *, temporal_client: Client | None, http_client: httpx.AsyncClient
) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if temporal_client is not None:
        extras[EXTRA_RESEARCH_STARTER] = TemporalResearchStarter(temporal_client)
    provider = _insight_provider(settings, http_client)
    if provider is not None:
        extras[EXTRA_INSIGHT_PROVIDER] = provider
    return extras


def _insight_provider(
    settings: Settings, http_client: httpx.AsyncClient
) -> YouTubeVideoInsightProvider | None:
    if not settings.youtube_analytics_enabled:
        return None
    if not (
        settings.youtube_client_id
        and settings.youtube_client_secret
        and settings.youtube_refresh_token_path
    ):
        logger.warning("agent analytics tools off: YouTube OAuth settings are incomplete")
        return None
    try:
        credentials = RefreshTokenCredentials.from_file(
            settings.youtube_client_id,
            settings.youtube_client_secret,
            settings.youtube_refresh_token_path,
            client=http_client,
        )
    except YouTubeAuthError as exc:
        logger.warning("agent analytics tools off: %s", exc)
        return None
    return YouTubeVideoInsightProvider(credentials, client=http_client)
