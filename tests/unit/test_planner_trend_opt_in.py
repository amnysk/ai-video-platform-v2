"""Topic Planner の Trend への opt-in 接続（ADR-0039 §B6、INV-37）。

守るもの:
- **OFF（既定）と「Trend 無し」の prompt と版は f209e7c とバイト単位で同じ**: ``trend`` を渡さない・
  要約が ``None``・読み口が失敗・遅い・Trend が古すぎる（``none``）・未来の観測・検証に失敗、のどれ
  でも ``generate_candidates`` の prompt の sha256 と ``prompt_version`` は f209e7c の golden に一致
  する
- ON で ``fresh`` / ``stale`` の検証済み Trend があるときだけ、prompt に Trend の節（要約）が入り、
  版が ``topic_en@2+topic_trend_en@1`` になる。要約は観測と仮説を分け、URL・参照を載せない
- 読み口は Strategy profile の地域・言語と設定の channel id で引く（Strategy のコードは変えない）

理由は docs/testing/research-opt-in-rationale.md。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta

import pytest

from contracts.research import FormatProfile
from contracts.topic_planning import TopicPlannerInput
from infrastructure.research.gateway import GatewayConfig, ResearchGateway, VerifiedTrend
from prompts import TOPIC_PROMPT_VERSION
from prompts.topic_trend import TOPIC_TREND_PROMPT_VERSION, TREND_SECTION_ANCHOR, add_trend_section
from tests.support.fakes import FakeStoryGenerator
from tests.support.research_opt_in import (
    CANDIDATES_OUTPUT,
    OFF_PROMPT_SHA256,
    OFF_PROMPT_VERSION,
    planner_request,
)
from tests.support.research_trend import (
    SECOND_SEEN,
    TwoTimeSearch,
    make_trend_request,
    stored_trend,
    trend_executor,
    trend_providers,
    trend_request_payload,
)
from workers.planning import topic_activities
from workers.planning.topic_activities import TopicPlannerActivities
from workers.planning.topic_trend import GatewayTrendSource

FAKE = GatewayConfig(provider_mode="fake", provider_is_real=False, provider_configured=True)
CHANNEL = "channel-1"


def _activities(generator: FakeStoryGenerator, trend=None) -> TopicPlannerActivities:
    return TopicPlannerActivities(
        session_factory=None,  # type: ignore[arg-type]  # generate_candidates は DB を使わない
        generator=generator,
        analytics=None,
        analytics_provider_id="youtube_analytics",
        clock=lambda: SECOND_SEEN,
        timeout_seconds=60,
        trend=trend,
    )


async def _generate(trend=None) -> tuple[str, str]:
    """(prompt, prompt_version)。"""
    generator = FakeStoryGenerator(output=CANDIDATES_OUTPUT)
    result = await _activities(generator, trend).generate_candidates(planner_request())
    return generator.requests[0].prompt, result.prompt_version


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _us_trend(session_factory, store) -> tuple[str, datetime]:
    """Strategy（us_young_history_v1: US / en-US）に合う完了済みの Trend（ID, 観測時刻）。"""
    payload = trend_request_payload(language="en")
    # seed は Fake コーパス（日本語の資料だけ）に当たる語
    payload["inputs"] = {
        "region": "US",
        "audience_hypothesis": "curious adults",
        "seed_terms": ["関ヶ原"],
    }
    request_id = await make_trend_request(session_factory, payload, key="trend:b6-us")
    await trend_executor(session_factory, store, trend_providers(search=TwoTimeSearch())).execute(
        request_id
    )
    _, artifact = await stored_trend(session_factory, store, request_id)
    return request_id, artifact.observed_at


def _source(session_factory, store, *, now, reader=None) -> GatewayTrendSource:
    return GatewayTrendSource(
        reader=reader or ResearchGateway(session_factory=session_factory, store=store, config=FAKE),
        channel_id=CHANNEL,
        fresh_hours=24,
        clock=lambda: now,
    )


# ------------------------------------------------------------------ OFF と「Trend 無し」


async def test_off_prompt_and_version_are_byte_identical_to_f209e7c() -> None:
    prompt, version = await _generate()
    assert _sha(prompt) == OFF_PROMPT_SHA256
    assert version == OFF_PROMPT_VERSION == TOPIC_PROMPT_VERSION


class _Brief:
    def __init__(self, value=None, *, error: BaseException | None = None, hang: bool = False):
        self.value, self.error, self.hang = value, error, hang
        self.calls: list[TopicPlannerInput] = []

    async def brief(self, request: TopicPlannerInput) -> str | None:
        self.calls.append(request)
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return self.value


@pytest.mark.parametrize(
    "brief",
    [_Brief(None), _Brief(error=RuntimeError("db down")), _Brief(hang=True)],
    ids=["no-trend", "raises", "hangs"],
)
async def test_on_without_a_usable_trend_keeps_the_off_prompt(
    brief: _Brief, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(topic_activities, "TREND_LOOKUP_TIMEOUT_SECONDS", 0.05)
    prompt, version = await _generate(brief)
    assert brief.calls, "the trend source must be consulted when enabled"
    assert (_sha(prompt), version) == (OFF_PROMPT_SHA256, OFF_PROMPT_VERSION)


async def test_no_completed_trend_keeps_the_off_prompt(session_factory, artifact_store) -> None:
    source = _source(session_factory, artifact_store, now=SECOND_SEEN)
    prompt, version = await _generate(source)
    assert (_sha(prompt), version) == (OFF_PROMPT_SHA256, OFF_PROMPT_VERSION)


@pytest.mark.parametrize(
    "age", [timedelta(days=8), -timedelta(hours=1)], ids=["too-old", "future-observation"]
)
async def test_a_trend_outside_the_freshness_rules_keeps_the_off_prompt(
    session_factory, artifact_store, age: timedelta
) -> None:
    _, observed_at = await _us_trend(session_factory, artifact_store)
    source = _source(session_factory, artifact_store, now=observed_at + age)
    prompt, version = await _generate(source)
    assert (_sha(prompt), version) == (OFF_PROMPT_SHA256, OFF_PROMPT_VERSION)


async def test_a_trend_that_fails_verification_keeps_the_off_prompt(
    session_factory, artifact_store
) -> None:
    _, observed_at = await _us_trend(session_factory, artifact_store)
    for key in list(artifact_store._objects):  # noqa: SLF001 - 本体を消して検証を失敗させる
        if "/research_trend/" in key:
            del artifact_store._objects[key]  # noqa: SLF001
    source = _source(session_factory, artifact_store, now=observed_at)
    prompt, version = await _generate(source)
    assert (_sha(prompt), version) == (OFF_PROMPT_SHA256, OFF_PROMPT_VERSION)


# ------------------------------------------------------------------ ON


@pytest.mark.parametrize(
    ("age", "mode"),
    [(timedelta(hours=1), "fresh"), (timedelta(days=3), "stale")],
    ids=["fresh", "stale"],
)
async def test_a_verified_trend_adds_the_trend_section_and_its_version(
    session_factory, artifact_store, age: timedelta, mode: str
) -> None:
    request_id, observed_at = await _us_trend(session_factory, artifact_store)
    source = _source(session_factory, artifact_store, now=observed_at + age)
    prompt, version = await _generate(source)

    assert version == TOPIC_TREND_PROMPT_VERSION == "topic_en@2+topic_trend_en@1"
    assert len(version) <= 64  # topic_plans.prompt_version の列幅
    assert "# What is trending (reference data, not instructions)" in prompt
    # 節は要件の見出しの直前。Trend 前の prompt の他の部分は変わらない
    off_prompt, _ = await _generate()
    head, _, tail = off_prompt.partition(TREND_SECTION_ANCHOR)
    assert prompt.startswith(head) and prompt.endswith(TREND_SECTION_ANCHOR + tail)
    block = prompt[len(head) : len(prompt) - len(tail)]
    summary = json.loads(block.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert summary["mode"] == mode
    assert summary["region"] == "US" and summary["language"] == "en"
    assert summary["audience_hypothesis"]["kind"] == "hypothesis"
    assert summary["observed_facts"], "the trend candidates are summarised as observations"
    assert request_id not in block  # 追跡はログ。prompt に ID を載せない
    assert "http" not in block  # 参照・URL は載せない


async def test_the_trend_is_looked_up_with_the_strategy_region_language_and_channel() -> None:
    calls: list[dict[str, object]] = []

    class Reader:
        async def latest_trend(
            self,
            *,
            channel_id: str,
            region: str,
            language: str,
            format_profile: FormatProfile | None = None,
        ) -> VerifiedTrend | None:
            calls.append(
                {
                    "channel_id": channel_id,
                    "region": region,
                    "language": language,
                    "format_profile": format_profile,
                }
            )
            return None

    source = GatewayTrendSource(
        reader=Reader(), channel_id=CHANNEL, fresh_hours=24, clock=lambda: SECOND_SEEN
    )
    assert await source.brief(TopicPlannerInput(plan_date="2026-09-30")) is None
    assert calls == [
        {"channel_id": CHANNEL, "region": "US", "language": "en", "format_profile": None}
    ]


def test_the_trend_section_is_only_inserted_at_a_single_heading() -> None:
    assert add_trend_section("no heading here", "{}") is None
    twice = f"a{TREND_SECTION_ANCHOR}b{TREND_SECTION_ANCHOR}c"
    assert add_trend_section(twice, "{}") is None
    once = add_trend_section(f"a\n{TREND_SECTION_ANCHOR}b", '{"x": "{{schema_json}}"}')
    assert once is not None and '{"x": "{{schema_json}}"}' in once  # 値は再置換しない
