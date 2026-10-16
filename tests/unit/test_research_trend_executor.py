"""Trend Research を実行器で走らせる（ADR-0039）。SQLite・インメモリの ArtifactStore・Fake。

守るもの:
- 検索と解釈は台帳を通る（解釈は ``assess`` 行。枠 ``max_assessments``。INV-36）。再実行は保存済みの
  提案を読み、解釈器を呼び直さない。Trend は本文を取得しない（``fetch`` 行が無い）
- 観測と解釈は別の欄。差分の増加速度は同じ動画を 2 時点で観測したときだけ。1 時点なら参考の平均
- 悪い提案（存在しない観測 ID・別の候補の観測・スコアの主張・参考値を「直近の伸び」と呼ぶ・
  型に無い欄）は採用せず、Trend は観測だけの ``partial``（合格にしない）
- 解釈器が無い・枠が 0 に近い・検索が落ちた Trend は ``completed`` にならない
- 動画の長さから Shorts と推定しない

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

import pytest

from contracts.research import ResearchCall, ResearchCallStatus, ResearchStatus
from contracts.research_trend import MetricStatus, ObservationMethod, StatMetric
from infrastructure.db.research_repositories import ResearchCallRepository
from infrastructure.research.errors import ProviderTransient
from infrastructure.research.fake_corpus import YT_SEKIGAHARA_LONG, YT_SEKIGAHARA_SHORT
from infrastructure.research.fake_providers import FakeSearchProvider
from infrastructure.research.fake_trend import BadInterpretation, FakeTrendInterpreter
from tests.support.research_trend import (
    FIRST_SEEN,
    SECOND_SEEN,
    SHORT_VIEWS_FIRST,
    SHORT_VIEWS_SECOND,
    TwoTimeSearch,
    make_trend_request,
    stored_trend,
    trend_executor,
    trend_providers,
    trend_request_payload,
)


async def _calls(session_factory, request_id: str):
    async with session_factory() as session:
        return await ResearchCallRepository(session).list_for_request(request_id)


def _candidate(artifact, url: str):
    return next(c for c in artifact.candidates if c.references[0].url == url)


# ------------------------------------------------------------------ 正常系


async def test_trend_runs_searches_and_one_interpretation_through_the_ledger(
    session_factory, artifact_store
) -> None:
    interpreter = FakeTrendInterpreter()
    request_id = await make_trend_request(session_factory)
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(interpreter=interpreter)
    ).execute(request_id)

    assert outcome.status is ResearchStatus.COMPLETED, outcome
    calls = await _calls(session_factory, request_id)
    assert all(c.status is ResearchCallStatus.SPENT and c.dispatched_at for c in calls)
    assert {c.call for c in calls} == {ResearchCall.SEARCH, ResearchCall.ASSESS}
    assess = [c for c in calls if c.call is ResearchCall.ASSESS]
    assert len(assess) == len(interpreter.calls) == outcome.usage.assessments == 1
    assert outcome.usage.fetches == 0  # Trend は本文を取得しない

    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert artifact.request_id == request_id
    assert artifact.observations and artifact.interpretations
    observed = {o.observation_id for o in artifact.observations}
    for interpretation in artifact.interpretations:
        assert interpretation.kind == "hypothesis"
        assert set(interpretation.basis_observation_ids) <= observed
    assert artifact.audience_hypothesis.measured is False
    assert (artifact.format_basis, artifact.format_confidence.value) == ("requested", "low")


async def test_the_delta_needs_the_same_video_observed_at_two_times(
    session_factory, artifact_store
) -> None:
    """2 回の検索で同じ動画を別の時点に観測したら差分、1 時点だけの動画は参考の平均。"""
    request_id = await make_trend_request(session_factory)
    search = TwoTimeSearch()
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(search=search)
    ).execute(request_id)
    assert outcome.status is ResearchStatus.COMPLETED

    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    short = _candidate(artifact, YT_SEKIGAHARA_SHORT)
    growth = short.metrics.growth_observation
    assert growth.metric is StatMetric.VIEWS_PER_HOUR_DELTA
    assert growth.value == pytest.approx((SHORT_VIEWS_SECOND - SHORT_VIEWS_FIRST) / 10)
    assert growth.observed_at == SECOND_SEEN
    views = [
        o
        for o in artifact.observations
        if o.candidate_id == short.candidate_id and o.metric is StatMetric.VIEWS_TOTAL
    ]
    assert [(o.value, o.observed_at) for o in views] == [
        (SHORT_VIEWS_FIRST, FIRST_SEEN),
        (SHORT_VIEWS_SECOND, SECOND_SEEN),
    ]
    delta = [o for o in artifact.observations if o.method is ObservationMethod.DELTA]
    assert [o.candidate_id for o in delta] == [short.candidate_id]

    long = _candidate(artifact, YT_SEKIGAHARA_LONG)  # 30 日の窓でしか見えない（1 時点）
    assert long.metrics.growth_observation.metric is StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR
    assert "growth_is_lifetime_average" in {lim.code for lim in artifact.limitations}


async def test_one_observation_time_never_yields_a_delta(session_factory, artifact_store) -> None:
    """同じ動画が 2 回出ても観測時刻が同じなら差分は作らない（参考の平均だけ）。"""
    request_id = await make_trend_request(session_factory)
    await trend_executor(session_factory, artifact_store).execute(request_id)
    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert all(o.metric is not StatMetric.VIEWS_PER_HOUR_DELTA for o in artifact.observations)
    short = _candidate(artifact, YT_SEKIGAHARA_SHORT)
    assert short.metrics.growth_observation.metric is StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR


async def test_missing_values_are_unknown_with_a_reason_and_shorts_is_not_inferred(
    session_factory, artifact_store
) -> None:
    request_id = await make_trend_request(session_factory)
    await trend_executor(session_factory, artifact_store).execute(request_id)
    _, artifact = await stored_trend(session_factory, artifact_store, request_id)

    short = _candidate(artifact, YT_SEKIGAHARA_SHORT)  # 30 秒の動画・登録者数は非公開
    scale = short.metrics.channel_scale
    assert scale.status is MetricStatus.UNKNOWN and scale.value is None
    assert "subscriber" in (scale.unknown_reason or "")
    assert not any(
        o.candidate_id == short.candidate_id and o.metric is StatMetric.SUBSCRIBER_COUNT
        for o in artifact.observations
    )
    dumped = short.model_dump_json().lower()
    assert "shorts" not in dumped and "duration" not in dumped
    assert "video_duration_not_shorts" in {lim.code for lim in artifact.limitations}
    assert short.references[0].channel_id == "UChist200000000000000000"


async def test_a_rerun_reads_the_saved_proposal_and_does_not_interpret_again(
    session_factory, artifact_store
) -> None:
    request_id = await make_trend_request(session_factory)
    first = FakeTrendInterpreter()
    executor = trend_executor(session_factory, artifact_store, trend_providers(interpreter=first))
    original = executor._finalize

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after the calls")

    executor._finalize = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await executor.execute(request_id)
    executor._finalize = original  # type: ignore[method-assign]

    second = FakeTrendInterpreter()
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(interpreter=second)
    ).execute(request_id)
    assert outcome.status is ResearchStatus.COMPLETED
    assert len(first.calls) == 1 and second.calls == []
    assess = [c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS]
    assert len(assess) == 1


# ------------------------------------------------------------------ 悪い提案・欠け → partial


@pytest.mark.parametrize(
    "bad",
    [
        BadInterpretation.UNKNOWN_OBSERVATION_ID,
        BadInterpretation.OTHER_CANDIDATE_OBSERVATION,
        BadInterpretation.SCORE_IN_TEXT,
        BadInterpretation.RECENT_GROWTH_CLAIM,
        BadInterpretation.UNKNOWN_ANGLE_KEY,
    ],
)
async def test_a_bad_proposal_is_not_adopted_and_the_trend_is_partial(
    session_factory, artifact_store, bad
) -> None:
    request_id = await make_trend_request(session_factory)
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(interpreter=FakeTrendInterpreter(bad=bad))
    ).execute(request_id)

    assert outcome.status is ResearchStatus.PARTIAL
    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert artifact.observations  # 事実は残る
    assert artifact.interpretations == () and artifact.suggested_angles == ()


@pytest.mark.parametrize(
    "bad", [BadInterpretation.OVERALL_SCORE, BadInterpretation.FABRICATED_OBSERVATION]
)
async def test_extra_fields_in_the_proposal_are_a_schema_violation(
    session_factory, artifact_store, bad
) -> None:
    """型に無い欄（総合スコア・観測の捏造）は実行器の schema 検査で落ち、提案は保存もされない。"""
    request_id = await make_trend_request(session_factory)
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(interpreter=FakeTrendInterpreter(bad=bad))
    ).execute(request_id)

    assert outcome.status is ResearchStatus.PARTIAL
    assess = [c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS]
    assert len(assess) == 1 and (assess[0].error_summary or "").startswith("permanent:")
    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert artifact.interpretations == ()
    assert "overall_score" not in str(artifact.model_dump())


async def test_without_an_interpreter_the_trend_is_observations_only_and_partial(
    session_factory, artifact_store
) -> None:
    request_id = await make_trend_request(session_factory)
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(with_interpreter=False)
    ).execute(request_id)

    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "assessor_not_available"
    assert not [
        c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS
    ]
    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert artifact.observations and artifact.interpretations == ()


async def test_the_interpretation_is_bounded_by_the_assessment_ceiling(
    session_factory, artifact_store
) -> None:
    """解釈 1 回も ``assess`` の枠を数える。retry で枠を使い切ったら解釈せずに ``partial``。"""
    payload = trend_request_payload(limits={"max_assessments": 1})
    request_id = await make_trend_request(session_factory, payload)
    interpreter = FakeTrendInterpreter()
    interpreter.fail_next(ProviderTransient("interpreter 503"))
    executor = trend_executor(
        session_factory, artifact_store, trend_providers(interpreter=interpreter)
    )
    with pytest.raises(Exception, match="transiently"):
        await executor.execute(request_id)  # 1 行目（spent・一時障害）
    outcome = await executor.execute(request_id)  # Temporal の retry 相当

    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "call_budget_exhausted"
    assess = [c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS]
    assert len(assess) == 1  # 枠 1 を超える行は作らない（INV-36）
    assert len(interpreter.calls) == 1


async def test_a_failed_search_makes_the_trend_partial(session_factory, artifact_store) -> None:
    from infrastructure.research.errors import ProviderRejected

    search = FakeSearchProvider()
    search.fail_next(ProviderRejected("bad query"))
    request_id = await make_trend_request(session_factory)
    outcome = await trend_executor(
        session_factory, artifact_store, trend_providers(search=search)
    ).execute(request_id)

    assert outcome.status is ResearchStatus.PARTIAL
    _, artifact = await stored_trend(session_factory, artifact_store, request_id)
    assert artifact.coverage.queries_completed < artifact.coverage.queries_planned
    assert any("search failed" in n.reason for n in artifact.coverage.not_retrieved)


async def test_provider_none_blocks_a_trend_before_any_call(
    session_factory, artifact_store
) -> None:
    from infrastructure.research.registry import ResearchProviders

    request_id = await make_trend_request(session_factory)
    none = ResearchProviders(mode="none", search=None, fetcher=None, is_real=False)
    outcome = await trend_executor(session_factory, artifact_store, none).execute(request_id)
    assert outcome.status is ResearchStatus.BLOCKED
    assert outcome.stop_code == "provider_not_configured"
    assert await _calls(session_factory, request_id) == []
