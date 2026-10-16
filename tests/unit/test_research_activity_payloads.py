"""Research の workflow / Activity 境界を Temporal の既定 converter で往復させる（ADR-0029）。

境界の型は ``contracts/research.py`` の dataclass だけ。実際の形（非空の参照・件数・金額の文字列）で
往復させ、復号できることと、成果物の本体を載せる欄が無いことを固定する。

理由は docs/testing/research-worker-rationale.md。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest
from temporalio.converter import DataConverter

from contracts import research as research_contracts
from contracts.research import (
    ResearchArtifactPointer,
    ResearchArtifactRef,
    ResearchArtifactType,
    ResearchExecuteRequest,
    ResearchRecordFailureRequest,
    ResearchStatus,
    ResearchUsage,
    ResearchWorkflowInput,
    ResearchWorkflowOutput,
    research_workflow_id,
)
from infrastructure.research.executor import ResearchExecution
from workers.research.activities import to_output

REQUEST_ID = "7d1c3f5e-8a6b-4c2d-9e0f-1a2b3c4d5e6f"
SHA = "a" * 64


def roundtrip(value: Any) -> Any:
    converter = DataConverter.default.payload_converter
    return converter.from_payload(converter.to_payload(value), type(value))


SAMPLES: list[Any] = [
    ResearchWorkflowInput(request_id=REQUEST_ID),
    ResearchExecuteRequest(request_id=REQUEST_ID),
    ResearchRecordFailureRequest(
        request_id=REQUEST_ID, error_type="ResearchSourceUnavailableError", summary="reset"
    ),
    ResearchRecordFailureRequest(request_id=REQUEST_ID, error_type=None, summary=""),
    ResearchWorkflowOutput(
        request_id=REQUEST_ID,
        status="partial",
        stop_code="call_budget_exhausted",
        artifact_refs=[
            ResearchArtifactPointer(
                artifact_type="research_evidence", artifact_id=REQUEST_ID, sha256=SHA
            )
        ],
        searches=5,
        fetches=10,
        assessments=1,
        youtube_units=500,
        cost_usd="0.1250",
    ),
    ResearchWorkflowOutput(request_id=REQUEST_ID, status="blocked"),
]


@pytest.mark.parametrize("value", SAMPLES, ids=lambda v: type(v).__name__)
def test_every_boundary_type_roundtrips_through_the_default_converter(value: Any) -> None:
    assert roundtrip(value) == value


def test_every_boundary_dataclass_in_the_contract_is_sampled() -> None:
    """境界の dataclass を足したら、ここにも実際の形の見本を足す（漏れの検査）。"""
    boundary = {
        name
        for name in research_contracts.__all__
        if dataclasses.is_dataclass(getattr(research_contracts, name))
    }
    sampled = {type(v).__name__ for v in SAMPLES} | {"ResearchArtifactPointer"}
    assert boundary == sampled


def test_the_output_carries_only_refs_and_counts() -> None:
    names = {f.name for f in dataclasses.fields(ResearchWorkflowOutput)}
    assert names == {
        "request_id",
        "status",
        "stop_code",
        "artifact_refs",
        "searches",
        "fetches",
        "assessments",
        "youtube_units",
        "cost_usd",
    }
    assert {f.name for f in dataclasses.fields(ResearchArtifactPointer)} == {
        "artifact_type",
        "artifact_id",
        "sha256",
    }


def test_the_executor_outcome_maps_onto_the_boundary_shape() -> None:
    execution = ResearchExecution(
        request_id=REQUEST_ID,
        status=ResearchStatus.PARTIAL,
        artifact_refs=(
            ResearchArtifactRef(
                artifact_type=ResearchArtifactType.RESEARCH_TREND,
                artifact_id=REQUEST_ID,
                sha256=SHA,
            ),
        ),
        stop_code="rate_limited",
        usage=ResearchUsage(searches=2, fetches=3, youtube_units=200, cost_usd=Decimal("0.5")),
    )
    output = to_output(execution)
    assert output == ResearchWorkflowOutput(
        request_id=REQUEST_ID,
        status="partial",
        stop_code="rate_limited",
        artifact_refs=[
            ResearchArtifactPointer(
                artifact_type="research_trend", artifact_id=REQUEST_ID, sha256=SHA
            )
        ],
        searches=2,
        fetches=3,
        assessments=0,
        youtube_units=200,
        cost_usd="0.5",
    )
    assert roundtrip(output) == output


def test_the_workflow_id_is_one_per_request_and_rejects_non_uuids() -> None:
    assert research_workflow_id(REQUEST_ID) == f"research-{REQUEST_ID}"
    with pytest.raises(ValueError):
        research_workflow_id("not-a-uuid")
    with pytest.raises(ValueError):
        research_workflow_id(REQUEST_ID.upper())  # 正準形だけ（同じ依頼に 2 つの id を作らない）
