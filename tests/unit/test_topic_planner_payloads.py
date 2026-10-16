"""Topic Planner の Activity 境界を、Temporal の既定 payload converter で往復させる（ADR-0029）。

fake provider の出力は形が単純で、型注釈と実 payload の形の食い違い（実 Analytics だけが返す
``age_groups`` などの dict）を隠した。ここでは「実 API と同じ形の非空の入れ子」を境界の型ごとに
往復させ、復号できることを固定する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from temporalio.converter import DataConverter

from contracts.topic_planning import (
    AnalyticsMode,
    AnalyticsSummary,
    FeaturePerformance,
    FindPlanRequest,
    FindPlanResult,
    GenerateCandidatesRequest,
    GenerateCandidatesResult,
    MemoryItem,
    PlanningContext,
    SelectAndSaveRequest,
    SelectAndSaveResult,
    TopicPlannerInput,
    TopicPlannerResult,
)
from domain.topic_planning import AnalyticsReport, AudienceShares, VideoMetrics
from workers.planning import topic_activities
from workers.planning.topic_activities import (
    ANALYTICS_WINDOWS,
    TopicPlannerActivities,
    ensure_decodable,
)

REQUEST = TopicPlannerInput(plan_date="2026-09-20")


def roundtrip(value: Any) -> Any:
    """Workflow / Activity の境界と同じ経路（既定 converter。型注釈で復号する）。"""
    converter = DataConverter.default.payload_converter
    return converter.from_payload(converter.to_payload(value), type(value))


def real_shaped_report() -> AnalyticsReport:
    """実 YouTube Analytics が返す形（audience は全項目が埋まり、dict を含む）。数値は合成。"""
    metrics = [
        VideoMetrics(f"vid{i}", 5000.0 + i, 100.0, 30.0, 80.0, 10.0, 2.0, 1.0, 0.0)
        for i in range(3)
    ]
    return AnalyticsReport(
        videos_by_window={w: metrics for w in sorted(ANALYTICS_WINDOWS)},
        audience=AudienceShares(
            country_us=0.41,
            age_18_24=0.22,
            age_25_34=0.31,
            shorts=0.9,
            age_groups={"age13-17": 0.05, "age18-24": 0.22, "age25-34": 0.31},
            genders={"female": 0.3, "male": 0.7},
            countries={"US": 0.41, "JP": 0.2},
            content_types={"SHORTS": 0.9, "VIDEO_ON_DEMAND": 0.1},
        ),
    )


class _Analytics:
    def __init__(self, report: AnalyticsReport) -> None:
        self.report = report

    async def fetch(self, window_days):  # noqa: ANN001
        return self.report


def make_activities(session_factory, report: AnalyticsReport | None) -> TopicPlannerActivities:
    from tests.support.fakes import FakeStoryGenerator

    return TopicPlannerActivities(
        session_factory=session_factory,
        generator=FakeStoryGenerator(),
        analytics=_Analytics(report) if report is not None else None,
        analytics_provider_id="youtube_analytics",
        clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        timeout_seconds=60,
    )


@pytest.mark.asyncio
async def test_gather_context_with_real_shaped_audience_survives_the_converter(
    session_factory,
) -> None:
    """2026-09-20 の E2E で止まった形: audience の内訳(dict)が totals に入り復号できなかった。"""
    acts = make_activities(session_factory, real_shaped_report())
    context = await acts.gather_context(REQUEST)

    assert context.analytics.mode == AnalyticsMode.NORMAL.value
    decoded = roundtrip(context)
    assert decoded == context
    # 内訳は失われず、構造化されたまま prompt / 採点から読める
    assert decoded.analytics.audience["age_groups"]["age18-24"] == pytest.approx(0.22)
    assert decoded.analytics.audience["genders"]["male"] == pytest.approx(0.7)
    assert decoded.analytics.audience["summary"]["country_us"] == pytest.approx(0.41)
    # totals は「窓 → 指標 → 数値」だけ（audience を混ぜない）
    assert set(decoded.analytics.totals) == {"7d", "28d", "90d"}


@pytest.mark.parametrize(
    ("audience", "groups"),
    [
        (AudienceShares(country_us=0.4, age_18_24=0.3), {"summary"}),
        (AudienceShares(age_groups={"age18-24": 1.0}), {"age_groups"}),
        (AudienceShares(), set()),
    ],
    ids=["scalars-only", "breakdown-only", "empty"],
)
@pytest.mark.asyncio
async def test_gather_context_with_partial_audience_survives_the_converter(
    session_factory, audience: AudienceShares, groups: set[str]
) -> None:
    """取れなかった項目は None。scalar だけ・内訳だけ・空でも往復できる。

    同じ日の snapshot は再利用される（save が既存を返す）ので、ケースごとに DB を分ける。
    """
    report = replace(real_shaped_report(), audience=audience)
    context = await make_activities(session_factory, report).gather_context(REQUEST)
    assert roundtrip(context) == context
    assert set(context.analytics.audience) == groups


@pytest.mark.asyncio
async def test_a_context_the_workflow_could_not_decode_degrades_to_no_analytics(
    session_factory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """復号できない形は workflow task の無限失敗になる。Activity の側で劣化させて止める。

    Analytics は best-effort（ADR-0025）。fallback の梯子は Activity の例外しか捕まえず、
    workflow 側の復号失敗には届かないため、返す直前に往復を確かめる。
    """
    monkeypatch.setattr(
        topic_activities,
        "_totals",
        lambda report: {"7d": {"views": {"nested": 1.0}}},  # 型注釈（float）と実際の形が違う
    )
    acts = make_activities(session_factory, real_shaped_report())
    with caplog.at_level("WARNING"):
        context = await acts.gather_context(REQUEST)

    assert context.analytics.mode == AnalyticsMode.NO_ANALYTICS.value
    assert context.analytics.totals == {}
    assert context.analytics.features == []
    assert roundtrip(context) == context
    assert "undecodable" in caplog.text
    # 実値・例外文を書かない（INV-20）。型名だけ
    assert "nested" not in caplog.text


def test_ensure_decodable_keeps_a_good_context_unchanged() -> None:
    ctx = PlanningContext(
        request=REQUEST,
        analytics=AnalyticsSummary(mode="normal", totals={"7d": {"views": 1.0}}),
        memory=[],
    )
    assert ensure_decodable(ctx) is ctx


# ---------------------------------------------------- 境界の型ごとの往復（実際の形・非空）


def _context() -> PlanningContext:
    return PlanningContext(
        request=REQUEST,
        analytics=AnalyticsSummary(
            mode="normal",
            snapshot_id="00000000-0000-0000-0000-000000000001",
            confidence=0.5,
            features=[FeaturePerformance("theme:ritual_and_religion", 1.2, 3)],
            totals={"7d": {"videos": 3.0, "views": 15003.0}},
            audience={"summary": {"country_us": 0.4}, "age_groups": {"age18-24": 0.22}},
        ),
        memory=[
            MemoryItem(
                topic="Why sumo wrestlers throw salt",
                subject="sumo_salt",
                entities=["sumo", "Shinto"],
                era="edo",
                theme="ritual_and_religion",
                angle="myth_busting",
                day="2026-09-19",
                status="planned",
            )
        ],
    )


CANDIDATE = {
    "topic": "Why sumo wrestlers throw salt",
    "subject": "sumo_salt",
    "entities": ["sumo"],
    "score": None,
}

BOUNDARY_VALUES = {
    "TopicPlannerInput": TopicPlannerInput(plan_date="2026-09-20"),
    "TopicPlannerResult": TopicPlannerResult("id-1", "A topic", False, "normal"),
    "FindPlanRequest": FindPlanRequest("2026-09-20", "us_young_history_v1", "shorts"),
    "FindPlanResult": FindPlanResult("id-1", "A topic", "normal"),
    "PlanningContext": _context(),
    "GenerateCandidatesRequest": GenerateCandidatesRequest(_context(), 2, ["sumo_salt"]),
    "GenerateCandidatesResult": GenerateCandidatesResult([CANDIDATE], "topic-v1"),
    "SelectAndSaveRequest": SelectAndSaveRequest(_context(), [CANDIDATE], "topic-v1", 2),
    "SelectAndSaveResult": SelectAndSaveResult("id-1", "A topic", False, ["tea_ceremony"]),
}


@pytest.mark.parametrize("name", sorted(BOUNDARY_VALUES))
def test_every_topic_planner_boundary_type_round_trips(name: str) -> None:
    value = BOUNDARY_VALUES[name]
    assert roundtrip(value) == value


def test_boundary_table_covers_every_dataclass_in_the_contract() -> None:
    """契約に型を足したら、ここへも足す（往復の検査から漏れない）。"""
    import contracts.topic_planning as contract

    dataclass_names = {
        n
        for n, obj in vars(contract).items()
        if isinstance(obj, type)
        and hasattr(obj, "__dataclass_fields__")
        and obj.__module__ == contract.__name__
        and n not in {"AnalyticsSummary", "FeaturePerformance", "MemoryItem"}  # PlanningContext 内
    }
    assert dataclass_names <= set(BOUNDARY_VALUES)


# ------------------------------------------------------------ 保存済み snapshot の互換と劣化


def test_snapshot_payload_round_trips_the_full_audience() -> None:
    report = real_shaped_report()
    assert (
        topic_activities.report_from_payload(topic_activities.report_to_payload(report)) == report
    )


def test_snapshot_saved_before_the_breakdown_fields_still_loads() -> None:
    """内訳の項目が増える前に保存された snapshot（鍵が無い）も読める。"""
    payload = {
        "videos_by_window": {},
        "audience": {"country_us": 0.4, "age_18_24": 0.3, "age_25_34": None, "shorts": None},
    }
    report = topic_activities.report_from_payload(payload)
    assert report.audience == AudienceShares(country_us=0.4, age_18_24=0.3)


@pytest.mark.asyncio
async def test_an_unreadable_saved_snapshot_degrades_to_no_analytics(session_factory) -> None:
    """壊れた保存済み payload で Activity が例外を出し続けると Planner が止まる。劣化させる。"""
    from infrastructure.db.repositories import AnalyticsSnapshotRepository

    async with session_factory() as s:
        await AnalyticsSnapshotRepository(s).save(
            datetime(2026, 9, 10, tzinfo=UTC).date(),
            "youtube_analytics",
            {"videos_by_window": {"7": [{"unknown_metric": 1}]}, "audience": {"nope": 1}},
        )
        await s.commit()
    context = await make_activities(session_factory, None).gather_context(REQUEST)
    assert context.analytics.mode == AnalyticsMode.NO_ANALYTICS.value
    assert context.analytics.snapshot_id is None
