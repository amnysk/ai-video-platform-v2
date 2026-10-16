"""Research の契約（ADR-0037）: 依頼・上限・結果・語彙。

理由は docs/testing/research-persistence-rationale.md。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from contracts.research import (
    LIMIT_CEILING_SEARCHES,
    EvidenceResearchRequest,
    ResearchArtifactType,
    ResearchCall,
    ResearchCallStatus,
    ResearchKind,
    ResearchLimits,
    ResearchResult,
    ResearchStatus,
    TrendResearchRequest,
    call_ceiling,
    parse_research_spec,
    parse_research_submit,
    refresh_slot_of,
    submit_to_spec,
)
from tests.support.research import AS_OF, evidence_payload, trend_payload


def test_vocabularies_are_research_only_and_do_not_touch_production_states() -> None:
    """INV-37: research の語彙を本番の JobType / ArtifactType / ProviderCall に入れない。"""
    from contracts.states import ArtifactType, JobType, ProviderCall

    production = {v.value for v in (*JobType, *ArtifactType, *ProviderCall)}
    research = {
        v.value for v in (*ResearchKind, *ResearchStatus, *ResearchCall, *ResearchArtifactType)
    }
    assert production.isdisjoint(research)
    assert {k.value for k in ResearchKind} == {"trend", "evidence"}
    assert {s.value for s in ResearchStatus} == {
        "queued",
        "running",
        "completed",
        "partial",
        "blocked",
        "failed",
    }
    assert {c.value for c in ResearchCallStatus} == {"reserved", "spent", "abandoned"}
    assert {c.value for c in ResearchCall} == {"search", "fetch", "assess"}
    assert {a.value for a in ResearchArtifactType} == {
        "research_trend",
        "research_evidence",
        "research_script_verification",
    }


def test_the_kind_discriminates_the_request_and_rejects_cross_kind_inputs() -> None:
    assert isinstance(parse_research_spec(trend_payload()), TrendResearchRequest)
    assert isinstance(parse_research_spec(evidence_payload()), EvidenceResearchRequest)
    with pytest.raises(ValidationError):
        parse_research_spec(trend_payload(inputs=evidence_payload()["inputs"]))
    with pytest.raises(ValidationError):
        parse_research_spec(trend_payload(kind="strategy"))
    with pytest.raises(ValidationError):
        parse_research_spec(trend_payload(unexpected="x"))


def test_as_of_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        parse_research_spec(trend_payload(as_of="2026-09-29T03:00:00"))


def test_time_window_must_be_ordered() -> None:
    window = {"start": AS_OF.isoformat(), "end": AS_OF.isoformat()}
    with pytest.raises(ValidationError):
        parse_research_spec(trend_payload(time_window=window))


def test_evidence_claims_are_unique_after_normalization() -> None:
    claims = [
        {"claim_text": "The bridge opened in 1883", "kind": "year", "importance": "central"},
        {"claim_text": "the  BRIDGE opened in 1883", "kind": "year", "importance": "central"},
    ]
    with pytest.raises(ValidationError):
        parse_research_spec(evidence_payload(inputs={"claim_inputs": claims}))


def test_limits_have_defaults_and_ceilings() -> None:
    limits = ResearchLimits()
    assert limits.max_cost_usd is None and limits.max_youtube_units is None
    with pytest.raises(ValidationError):
        ResearchLimits(max_searches=LIMIT_CEILING_SEARCHES + 1)
    with pytest.raises(ValidationError):
        ResearchLimits(max_fetches=0)
    with pytest.raises(ValidationError):
        ResearchLimits.model_validate({"max_searches": "3"})  # strict: 文字列の数は受けない


def test_every_call_kind_has_exactly_one_ceiling_from_the_frozen_limits() -> None:
    """INV-36: 呼び出し種別ごとの上限は依頼に凍結した limits から1通りに決まる。"""
    limits = ResearchLimits(max_searches=2, max_fetches=3, max_assessments=4)
    assert call_ceiling(limits, ResearchCall.SEARCH) == 2
    assert call_ceiling(limits, ResearchCall.FETCH) == 3
    assert call_ceiling(limits, ResearchCall.ASSESS) == 4
    assert {call_ceiling(ResearchLimits(), c) > 0 for c in ResearchCall} == {True}


def test_submit_carries_an_idempotency_key_that_is_not_part_of_the_spec() -> None:
    submit = parse_research_submit(trend_payload(idempotency_key="trend:channel-1:2026-09-29"))
    spec = submit_to_spec(submit)
    assert not hasattr(spec, "idempotency_key")
    assert spec == parse_research_spec(trend_payload())
    with pytest.raises(ValidationError):
        parse_research_submit(trend_payload(idempotency_key="has space"))


def test_refresh_slot_is_the_utc_date_of_as_of() -> None:
    jst = timezone(timedelta(hours=9))
    assert refresh_slot_of(datetime(2026, 9, 30, 8, 0, tzinfo=jst)) == "2026-09-29"
    with pytest.raises(ValueError):
        refresh_slot_of(datetime(2026, 9, 29, 8, 0))  # noqa: DTZ001 - naive を拒否する検査


def _ref(artifact_type: str = "research_trend") -> dict[str, str]:
    return {
        "artifact_type": artifact_type,
        "artifact_id": "3f6a1c52-9d0b-4b7e-8a54-6c1e2f7d9b30",
        "sha256": "a" * 64,
    }


def test_result_artifacts_must_match_the_execution_status() -> None:
    request_id = "0b1d2c3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
    coverage = {"items_requested": 1, "items_covered": 1}
    ok = ResearchResult.model_validate(
        {
            "request_id": request_id,
            "execution_status": "completed",
            "artifact_refs": [_ref()],
            "coverage": coverage,
        }
    )
    assert ok.execution_status is ResearchStatus.COMPLETED
    with pytest.raises(ValidationError):
        ResearchResult.model_validate(
            {"request_id": request_id, "execution_status": "completed", "coverage": coverage}
        )
    with pytest.raises(ValidationError):
        ResearchResult.model_validate(
            {
                "request_id": request_id,
                "execution_status": "blocked",
                "artifact_refs": [_ref()],
                "coverage": coverage,
            }
        )
    with pytest.raises(ValidationError):
        ResearchResult.model_validate(
            {
                "request_id": request_id,
                "execution_status": "completed",
                "artifact_refs": [_ref("script")],
                "coverage": coverage,
            }
        )


def test_usage_cost_is_a_decimal() -> None:
    result = ResearchResult.model_validate(
        {
            "request_id": "0b1d2c3e-4f50-4a6b-8c7d-9e0f1a2b3c4d",
            "execution_status": "failed",
            "coverage": {"items_requested": 1, "items_covered": 0},
            "usage": {"searches": 1, "cost_usd": "0.0100"},
        }
    )
    assert result.usage.cost_usd == Decimal("0.01")
