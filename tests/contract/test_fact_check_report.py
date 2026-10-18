"""FactCheckReport（ADR-0047、artifact type fact_check_report）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.agentic import FactCheckReport
from contracts.artifacts import ARTIFACT_MODELS
from contracts.states import ArtifactType


def _claim(**kw: object) -> dict[str, object]:
    return {"claim": "The bridge opened in 1883.", "assessment": "supported", **kw}


def test_the_type_is_registered_with_its_model() -> None:
    assert ARTIFACT_MODELS[ArtifactType.FACT_CHECK_REPORT] is FactCheckReport


def test_an_assessment_other_than_insufficient_needs_evidence() -> None:
    with pytest.raises(ValidationError):
        FactCheckReport.model_validate({"episode_id": "e", "claims": [_claim()]})
    ok = FactCheckReport.model_validate(
        {"episode_id": "e", "claims": [_claim(evidence_refs=["tool_call:abc"])]}
    )
    assert ok.untrusted is True
    # 根拠が無い claim は insufficient のまま報告できる（架空の根拠で埋めない。INV-58）
    FactCheckReport.model_validate(
        {"episode_id": "e", "claims": [_claim(assessment="insufficient")]}
    )


def test_the_report_is_strict_and_untrusted() -> None:
    with pytest.raises(ValidationError):
        FactCheckReport.model_validate({"episode_id": "e", "untrusted": False})
    with pytest.raises(ValidationError):
        FactCheckReport.model_validate({"episode_id": "e", "invented": 1})
    with pytest.raises(ValidationError):
        FactCheckReport.model_validate({"episode_id": "e", "claims": [_claim()] * 13})
