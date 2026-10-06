"""Topic Planner の Trend 参照（ADR-0039 §B6。opt-in・既定 OFF）。

``PLANNER_TREND_ENABLED`` のときだけ ``workers/planning/research_wiring.py`` が組み、
``TopicPlannerActivities(trend=...)`` に渡す。OFF のときこのモジュールは読み込まれない。

- **読むだけ**: ``ResearchGateway.latest_trend``（検証済みの最新 ``completed`` の Trend。
  どこで失敗しても ``None``）。Research を起動しない・待たない（INV-37）
- 鮮度は ``domain/research/trend_freshness.py``（ADR-0039 §2）: ``fresh`` と
  ``stale``（観測日時つき）を使い、``none``（Trend 無し・古すぎる・未来の観測）は「Trend 無し」
- prompt に載せるのは**小さな要約**（テーマ・指標の読み・仮説・切り口・限界）で、成果物
  そのものや URL・参照は載せない。文は切り詰める
- 何が起きても例外を出さない（``None`` =「Trend 無し」で Trend 前の prompt に戻る）
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from contracts.research import FormatProfile, ResearchLanguage
from contracts.research_trend import MetricReading, TrendArtifact
from contracts.topic_planning import STRATEGY_PROFILES, TopicPlannerInput
from domain.research.trend_freshness import (
    FreshnessVerdict,
    TrendFreshness,
    classify_trend_freshness,
)
from infrastructure.research.gateway import VerifiedTrend

__all__ = ["GatewayTrendSource", "LatestTrendReader", "trend_summary"]

logger = logging.getLogger(__name__)

#: 要約に載せる件数と 1 件の文字数の上限（prompt を膨らませない）
MAX_THEMES = 8
MAX_HYPOTHESES = 5
MAX_ANGLES = 5
MAX_LIMITATIONS = 5
MAX_TEXT_CHARS = 200

_RESEARCH_LANGUAGES: dict[str, ResearchLanguage] = {"ja": "ja", "en": "en"}


class LatestTrendReader(Protocol):
    """``ResearchGateway.latest_trend`` の形（テストは Fake を渡す）。"""

    async def latest_trend(
        self,
        *,
        channel_id: str,
        region: str,
        language: str,
        format_profile: FormatProfile | None = None,
    ) -> VerifiedTrend | None: ...


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1] + "…"


def _reading(reading: MetricReading) -> dict[str, Any]:
    data = reading.model_dump(mode="json", exclude_none=True)
    if "unknown_reason" in data:
        data["unknown_reason"] = _cut(data["unknown_reason"])
    return data


def trend_summary(trend: VerifiedTrend, verdict: FreshnessVerdict) -> str:
    """prompt に載せる要約（正準の JSON 文字列）。観測と仮説を別の欄に置く。"""
    artifact: TrendArtifact = trend.artifact
    age = verdict.age
    summary = {
        "mode": verdict.mode.value,
        "observed_at": artifact.observed_at.isoformat(),
        "age_hours": None if age is None else int(age.total_seconds() // 3600),
        "region": artifact.region,
        "region_meaning": artifact.region_meaning,
        "language": artifact.language,
        "format_profile": artifact.format_profile,
        "audience_hypothesis": {
            "text": _cut(artifact.audience_hypothesis.text),
            "kind": artifact.audience_hypothesis.kind,
        },
        "observed_facts": [
            {
                "theme": _cut(c.theme),
                "growth_observation": _reading(c.metrics.growth_observation),
                "age_since_publish": _reading(c.metrics.age_since_publish),
                "theme_fit": _reading(c.metrics.theme_fit),
            }
            for c in artifact.candidates[:MAX_THEMES]
        ],
        "hypotheses": [_cut(i.text) for i in artifact.interpretations[:MAX_HYPOTHESES]],
        "suggested_angles": [_cut(a.text) for a in artifact.suggested_angles[:MAX_ANGLES]],
        "limitations": [_cut(lim.text) for lim in artifact.limitations[:MAX_LIMITATIONS]],
    }
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)


class GatewayTrendSource:
    """``TopicPlannerActivities`` の ``TrendBriefSource`` の実装。"""

    def __init__(
        self,
        *,
        reader: LatestTrendReader,
        channel_id: str,
        fresh_hours: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._reader = reader
        self._channel_id = channel_id
        self._fresh_hours = fresh_hours
        self._clock = clock

    async def brief(self, request: TopicPlannerInput) -> str | None:
        try:
            return await self._brief(request)
        except Exception as exc:  # noqa: BLE001 - Trend は補助。型だけをログに残す（INV-20）
            logger.warning(
                "trend brief unavailable (%s); planning without trend", type(exc).__name__
            )
            return None

    async def _brief(self, request: TopicPlannerInput) -> str | None:
        strategy = STRATEGY_PROFILES[request.strategy_profile_id]
        language = _RESEARCH_LANGUAGES.get(strategy.language.split("-", 1)[0])
        if language is None:
            return None
        trend = await self._reader.latest_trend(
            channel_id=self._channel_id, region=strategy.market_country, language=language
        )
        if trend is None:
            logger.info("no verified trend for %s; planning without trend", request.plan_date)
            return None
        verdict = classify_trend_freshness(
            trend.observed_at, self._clock(), fresh_hours=self._fresh_hours
        )
        if verdict.mode is TrendFreshness.NONE:
            logger.info("trend %s is not usable (none); planning without trend", trend.request_id)
            return None
        # topic_plans に列を足さない（ADR-0039 §B6）。どの Trend を使ったかはこのログと
        # prompt_version（topic_en@2+topic_trend_en@1）で追う
        logger.info(
            "planning %s with %s trend %s (artifact %s sha256 %s)",
            request.plan_date,
            verdict.mode.value,
            trend.request_id,
            trend.artifact_ref.artifact_id,
            trend.artifact_ref.sha256,
        )
        return trend_summary(trend, verdict)
