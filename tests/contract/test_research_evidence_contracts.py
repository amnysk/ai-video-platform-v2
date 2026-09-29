"""Evidence と台本の照合の成果物の契約（ADR-0038 §1 / §4）。

契約の validator が参照整合性の最終防衛線であること（評価器・Handler がどんな出力をしても、
未確認の本文を根拠にした成果物・根拠の無い合格は作れない）を固定する。

理由は docs/testing/research-evidence-rationale.md。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from contracts.research import ResearchStatus
from contracts.research_evidence import (
    EXCERPT_MAX_CHARS,
    AssessmentProposal,
    build_evidence_artifact,
    build_script_verification_artifact,
    excerpt_digest,
    parse_evidence_artifact,
)
from domain.research.evidence_text import PASSAGE_MAX_CHARS

REQUEST_ID = "7a6c119d-bc25-5ce8-aa48-a0901a9844ae"
SHA = "a" * 64
EXCERPT = "1543年、種子島にポルトガル人が漂着した。"


def _claim(**overrides: Any) -> dict[str, Any]:
    claim = {
        "claim_id": "C-001",
        "text": "鉄砲伝来は1543年",
        "kind": "year",
        "importance": "supporting",
        "strong": False,
        "assessed": True,
        "assessment": "supported",
        "assessment_reason": "one confirmed source",
        "usable_expression": "鉄砲伝来は1543年とされる。",
    }
    claim.update(overrides)
    return claim


def _source(sid: str = "S-001", **overrides: Any) -> dict[str, Any]:
    source = {
        "source_id": sid,
        "url": f"https://history-{sid.lower()}.example/page",
        "title": "鉄砲伝来",
        "language": "ja",
        "retrieved_at": "2026-09-21T00:00:00Z",
        "content_sha256": SHA,
        "fetch_status": "fetched",
        "origin_key": f"origin-{sid}",
        "source_kind": "secondary",
    }
    source.update(overrides)
    return source


def _link(sid: str = "S-001", stance: str = "supports") -> dict[str, Any]:
    return {
        "claim_id": "C-001",
        "source_id": sid,
        "stance": stance,
        "locator": "第1段落・第1文",
        "excerpt": EXCERPT,
        "excerpt_sha256": excerpt_digest(EXCERPT),
    }


def _build(claims=None, sources=None, links=None) -> dict[str, Any]:
    return build_evidence_artifact(
        request_id=REQUEST_ID,
        as_of="2026-09-29T03:00:00Z",
        claims=claims if claims is not None else [_claim()],
        sources=sources if sources is not None else [_source()],
        links=links if links is not None else [_link()],
    )


def test_a_well_formed_artifact_round_trips() -> None:
    payload = _build()
    assert parse_evidence_artifact(payload).claims[0].claim_id == "C-001"


@pytest.mark.parametrize("status", ["truncated", "failed"])
def test_an_unconfirmed_body_cannot_back_a_claim(status: str) -> None:
    sha = SHA if status == "truncated" else None
    with pytest.raises(ValidationError, match="cannot be the basis"):
        _build(sources=[_source(fetch_status=status, content_sha256=sha)])


def test_an_unassessed_claim_can_only_be_insufficient_and_has_no_links() -> None:
    with pytest.raises(ValidationError, match="unassessed"):
        _build(claims=[_claim(assessed=False)])
    with pytest.raises(ValidationError, match="unassessed claim has no links"):
        _build(
            claims=[_claim(assessed=False, assessment="insufficient", usable_expression="")],
        )


def test_insufficient_carries_no_usable_expression() -> None:
    with pytest.raises(ValidationError, match="usable_expression"):
        _build(claims=[_claim(assessment="insufficient")], links=[])


def test_a_strong_claim_needs_two_independent_origins_and_an_authoritative_one() -> None:
    strong = _claim(kind="quantity", strong=True)
    with pytest.raises(ValidationError, match="independent origins"):
        _build(
            claims=[strong],
            sources=[_source("S-001"), _source("S-002", origin_key="origin-S-001")],
            links=[_link("S-001"), _link("S-002")],
        )
    with pytest.raises(ValidationError, match="primary, scholarly or institutional"):
        _build(
            claims=[strong],
            sources=[_source("S-001"), _source("S-002")],
            links=[_link("S-001"), _link("S-002")],
        )
    ok = _build(
        claims=[strong],
        sources=[_source("S-001"), _source("S-002", source_kind="institutional")],
        links=[_link("S-001"), _link("S-002")],
    )
    assert ok["claims"][0]["assessment"] == "supported"


def test_supported_with_a_refutation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="refutes"):
        _build(
            sources=[_source("S-001"), _source("S-002")],
            links=[_link("S-001"), _link("S-002", "refutes")],
        )


def test_source_urls_must_be_plain_http_urls() -> None:
    with pytest.raises(ValidationError):
        _build(sources=[_source(url="https://user:pw@history.example/x")])
    with pytest.raises(ValidationError):
        _build(sources=[_source(url="javascript:alert(1)")])


def test_the_excerpt_limit_matches_the_passage_limit() -> None:
    assert PASSAGE_MAX_CHARS == EXCERPT_MAX_CHARS


def test_the_assessment_proposal_is_a_strict_llm_output_schema() -> None:
    schema = AssessmentProposal.model_json_schema()
    objects = [schema, *(d for d in schema.get("$defs", {}).values() if d.get("type") == "object")]
    for obj in objects:
        assert set(obj["properties"]) == set(obj.get("required", []))
        assert obj.get("additionalProperties") is False


# ------------------------------------------------------------------ 照合


def _verification(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "request_id": REQUEST_ID,
        "source_evidence": {"artifact_id": REQUEST_ID, "sha256": SHA},
        "evidence_status": "completed",
        "language": "ja",
        "units": [
            {
                "unit_id": "scene:s1:narration",
                "text_sha256": SHA,
                "checks": [{"claim_id": "C-001", "outcome": "ok", "reason": ""}],
            }
        ],
        "verdict": "passed",
    }
    fields.update(overrides)
    return fields


def test_passed_needs_completed_evidence_and_every_check_ok() -> None:
    assert build_script_verification_artifact(**_verification())["verdict"] == "passed"
    for status in (ResearchStatus.PARTIAL, ResearchStatus.BLOCKED):
        with pytest.raises(ValidationError, match="completed"):
            build_script_verification_artifact(**_verification(evidence_status=status.value))
    bad_units = [
        {
            "unit_id": "hook",
            "text_sha256": SHA,
            "checks": [{"claim_id": "C-001", "outcome": "overstated", "reason": "all > some"}],
        }
    ]
    with pytest.raises(ValidationError, match="every check ok"):
        build_script_verification_artifact(**_verification(units=bad_units))
    unregistered = [{"unit_id": "scene:s1:narration", "sentence": "x", "reason": "no claim"}]
    with pytest.raises(ValidationError, match="unregistered"):
        build_script_verification_artifact(**_verification(unregistered=unregistered))


def test_a_non_passing_verdict_needs_reasons_and_known_units() -> None:
    with pytest.raises(ValidationError, match="reasons"):
        build_script_verification_artifact(**_verification(verdict="failed"))
    with pytest.raises(ValidationError, match="unknown unit"):
        build_script_verification_artifact(
            **_verification(
                verdict="failed",
                reasons=["1 unregistered claim(s)"],
                unregistered=[{"unit_id": "title", "sentence": "x", "reason": "no claim"}],
            )
        )
