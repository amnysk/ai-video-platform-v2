"""Trend の成果物の契約（ADR-0039 §1）。

契約の validator が最終防衛線であること（解釈器・Handler がどんな出力をしても、観測と解釈の混同・
存在しない観測を根拠にした解釈・欠損の 0 埋め・総合スコア・長さからの Shorts 推定・測定値の顔をした
視聴者仮説を持つ成果物は作れない）を固定する。

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from contracts.research_trend import (
    InterpretationProposal,
    TrendArtifact,
    build_trend_artifact,
    parse_trend_artifact,
)

REQUEST_ID = "7a6c119d-bc25-5ce8-aa48-a0901a9844ae"
AT = "2026-09-21T00:00:00Z"


def _unknown(reason: str = "not observed") -> dict[str, Any]:
    return {"status": "unknown", "unknown_reason": reason}


def _metrics(**overrides: Any) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "growth_observation": {
            "status": "known",
            "value": 12.5,
            "unit": "views/hour",
            "metric": "lifetime_average_views_per_hour",
            "observed_at": AT,
        },
        "channel_scale": _unknown("subscriber count is hidden"),
        "age_since_publish": _unknown(),
        "theme_fit": _unknown(),
        "difference_from_past": _unknown(),
        "evidence_availability": _unknown(),
    }
    metrics.update(overrides)
    return metrics


def _candidate(cid: str = "T-001", **overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "candidate_id": cid,
        "theme": "関ヶ原の戦い 30秒で分かる",
        "provider": "youtube",
        "references": [
            {
                "url": "https://www.youtube.com/watch?v=sekigahara02",
                "title": "関ヶ原の戦い 30秒で分かる",
                "channel_id": "UChist200000000000000000",
                "stats": [
                    {"metric": "views_total", "value": 980000, "unit": "views", "observed_at": AT}
                ],
            }
        ],
        "metrics": _metrics(),
    }
    candidate.update(overrides)
    return candidate


def _observation(oid: str = "O-001", cid: str = "T-001", **overrides: Any) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "observation_id": oid,
        "candidate_id": cid,
        "metric": "views_total",
        "value": 980000,
        "unit": "views",
        "observed_at": AT,
        "method": "reading",
    }
    observation.update(overrides)
    return observation


def _interpretation(basis: tuple[str, ...] = ("O-001",), **overrides: Any) -> dict[str, Any]:
    interpretation: dict[str, Any] = {
        "interpretation_id": "I-001",
        "candidate_id": "T-001",
        "text": "この題材に関心がある可能性がある（仮説）",
        "basis_observation_ids": list(basis),
    }
    interpretation.update(overrides)
    return interpretation


def _build(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "as_of": "2026-08-30T00:00:00Z",
        "observed_at": AT,
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-08-30T00:00:00Z"},
        "region": "JP",
        "audience_hypothesis": {"text": "日本史に関心のある成人"},
        "language": "ja",
        "format_profile": "shorts",
        "candidates": [_candidate()],
        "observations": [_observation()],
        "interpretations": [_interpretation()],
        "suggested_angles": [
            {
                "angle_id": "A-001",
                "candidate_id": "T-001",
                "text": "背景を別の視点で試す",
                "basis_interpretation_ids": ["I-001"],
            }
        ],
        "coverage": {"providers_used": ["youtube"], "queries_planned": 1, "queries_completed": 1},
    }
    fields.update(overrides)
    return build_trend_artifact(request_id=REQUEST_ID, **fields)


def test_a_well_formed_trend_round_trips() -> None:
    payload = _build()
    artifact = parse_trend_artifact(payload)
    assert artifact.model_dump(mode="json") == payload
    assert artifact.type.value == "research_trend"
    assert artifact.interpretations[0].kind == "hypothesis"


def test_an_interpretation_citing_an_unknown_observation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown observation O-999"):
        _build(interpretations=[_interpretation(("O-999",))])


def test_an_interpretation_citing_another_candidates_observation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="another candidate"):
        _build(
            candidates=[_candidate("T-001"), _candidate("T-002")],
            observations=[_observation("O-001", "T-002")],
        )


def test_an_interpretation_is_only_a_hypothesis_with_a_basis() -> None:
    with pytest.raises(ValidationError):
        _build(interpretations=[_interpretation(kind="fact")])
    with pytest.raises(ValidationError):
        _build(interpretations=[_interpretation(())])


def test_observations_carry_observed_at_and_a_method_named_by_the_metric() -> None:
    no_time = _observation()
    del no_time["observed_at"]
    with pytest.raises(ValidationError):
        _build(observations=[no_time])
    with pytest.raises(ValidationError, match="must be observed with method delta"):
        _build(observations=[_observation(metric="views_per_hour_delta", method="reading")])
    with pytest.raises(ValidationError, match="unknown candidate"):
        _build(observations=[_observation(cid="T-009")], interpretations=[], suggested_angles=[])


def test_an_unknown_metric_is_never_zero_and_needs_a_reason() -> None:
    with pytest.raises(ValidationError, match="never 0"):
        _build(
            candidates=[
                _candidate(metrics=_metrics(channel_scale={"status": "unknown", "value": 0}))
            ]
        )
    with pytest.raises(ValidationError, match="needs a reason"):
        _build(candidates=[_candidate(metrics=_metrics(channel_scale={"status": "unknown"}))])
    with pytest.raises(ValidationError, match="needs observed_at"):
        _build(
            candidates=[
                _candidate(metrics=_metrics(channel_scale={"status": "known", "value": 10}))
            ]
        )


def test_growth_must_name_delta_or_the_lifetime_average() -> None:
    views = {"status": "known", "value": 3, "unit": "views", "observed_at": AT}
    with pytest.raises(ValidationError, match="must name its metric"):
        _build(candidates=[_candidate(metrics=_metrics(growth_observation=views))])
    recent = {**views, "metric": "recent_growth"}
    with pytest.raises(ValidationError):
        _build(candidates=[_candidate(metrics=_metrics(growth_observation=recent))])


def test_there_is_no_single_composite_score_anywhere() -> None:
    """総合スコア・順位を置く欄が無い（欄名で検査。値の中身は Handler が検査する）。"""
    schema = str(TrendArtifact.model_json_schema()).lower()
    assert "score" not in schema and "rank" not in schema
    with pytest.raises(ValidationError):
        _build(candidates=[_candidate(overall_score=87)])
    with pytest.raises(ValidationError):
        _build(candidates=[_candidate(metrics={**_metrics(), "score": 0.9})])


def test_the_format_is_the_requested_value_with_low_confidence() -> None:
    """動画の長さから Shorts を推定しない: 形式は依頼の値で、確からしさは low に固定。"""
    artifact = parse_trend_artifact(_build())
    assert (artifact.format_basis, artifact.format_confidence.value) == ("requested", "low")
    with pytest.raises(ValidationError, match="low confidence"):
        _build(format_confidence="high")
    with pytest.raises(ValidationError):
        _build(format_basis="video_duration")
    with pytest.raises(ValidationError):  # 候補に形式・長さの欄は無い
        _build(candidates=[_candidate(format="shorts")])


def test_the_audience_is_a_hypothesis_not_a_measurement() -> None:
    artifact = parse_trend_artifact(_build())
    assert artifact.audience_hypothesis.kind == "hypothesis"
    assert artifact.audience_hypothesis.measured is False
    with pytest.raises(ValidationError):
        _build(audience_hypothesis={"text": "20代男性", "measured": True})
    with pytest.raises(ValidationError):
        _build(audience_hypothesis={"text": "20代男性", "kind": "measurement"})


def test_references_need_plain_urls_and_youtube_channel_ids() -> None:
    bad_url = _candidate()
    bad_url["references"] = [{**bad_url["references"][0], "url": "javascript:alert(1)"}]
    with pytest.raises(ValidationError):
        _build(candidates=[bad_url])
    bad_channel = _candidate()
    bad_channel["references"] = [{**bad_channel["references"][0], "channel_id": "UC_hist_1"}]
    with pytest.raises(ValidationError):
        _build(candidates=[bad_channel])


def test_an_angle_must_cite_a_known_interpretation() -> None:
    with pytest.raises(ValidationError, match="unknown interpretation I-002"):
        _build(
            suggested_angles=[
                {
                    "angle_id": "A-001",
                    "candidate_id": None,
                    "text": "根拠の無い切り口",
                    "basis_interpretation_ids": ["I-002"],
                }
            ]
        )


def test_the_interpretation_proposal_is_a_strict_llm_output_schema() -> None:
    """実 LLM の strict な出力 schema: 全 property required・extra 禁止（スコアの欄を足せない）。"""
    schema = InterpretationProposal.model_json_schema()
    for name, definition in {"InterpretationProposal": schema, **schema["$defs"]}.items():
        assert definition.get("additionalProperties") is False, name
        assert set(definition["required"]) == set(definition["properties"]), name
    good = {"interpretations": [], "angles": [], "unknowns": []}
    InterpretationProposal.model_validate(good)
    with pytest.raises(ValidationError):
        InterpretationProposal.model_validate({**good, "overall_score": 87})
    with pytest.raises(ValidationError):
        InterpretationProposal.model_validate({**good, "observations": [{"value": 1}]})
