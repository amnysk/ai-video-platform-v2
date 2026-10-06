"""台本の文面と Evidence の照合（ADR-0038 §4）。規則（純粋）と保存（``ScriptVerifier``）。

守るもの:
- ``passed`` は Evidence が ``completed`` で、全文面が根拠の範囲内で、未登録の主張が無いときだけ
- 強すぎる表現・異説を事実として述べる・未登録の主張は ``failed``
- 評価していない / ``insufficient`` の claim に依拠する、Evidence が ``completed`` でない場合は
  ``insufficient``（評価器なし・枠切れの Evidence で合格させない）
- 照合結果は Evidence の依頼が所有する research の成果物として、読み戻し照合の後に記録される。
  壊れた・現行でない Evidence では照合しない

理由は docs/testing/research-evidence-rationale.md。
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.research import ResearchArtifactType, ResearchStatus
from contracts.research_evidence import (
    ScriptVerificationRequest,
    VerificationVerdict,
    build_evidence_artifact,
    excerpt_digest,
    parse_evidence_artifact,
    parse_script_verification_artifact,
)
from domain.research.errors import ResearchArtifactReadbackError, ResearchInputInvalidError
from domain.research.evidence_ports import CandidateSentence
from domain.research.script_verification import verify_script
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.research.fake_evidence import FakeClaimExtractor
from infrastructure.research.verification import ScriptVerifier
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.research_evidence import (
    TANEGASHIMA_CLAIM,
    claims_payload,
    evidence_executor,
    evidence_providers,
    make_request,
    stored_evidence,
)

REQUEST_ID = "7a6c119d-bc25-5ce8-aa48-a0901a9844ae"
MEIJI = "明治維新は1868年に江戸幕府から明治政府へ政権が移った変革とされる。"
DISSENT = "諸説あり、鉄砲伝来は1543年とされるが1542年とする説もある。"


def _evidence() -> Any:
    excerpt = "1868年に江戸幕府から明治政府へ政権が移った"
    link = {
        "claim_id": "C-001",
        "source_id": "S-001",
        "stance": "supports",
        "locator": "第1段落・第1文",
        "excerpt": excerpt,
        "excerpt_sha256": excerpt_digest(excerpt),
    }
    dissent_link = {**link, "claim_id": "C-002"}
    payload = build_evidence_artifact(
        request_id=REQUEST_ID,
        as_of="2026-09-29T03:00:00Z",
        claims=[
            _claim("C-001", "明治維新は1868年に始まった", "supported", MEIJI),
            _claim("C-002", "鉄砲伝来は1543年", "disputed", DISSENT),
            _claim("C-003", "長篠の戦いは鉄砲の数で勝敗が決まった", "insufficient", ""),
        ],
        sources=[
            {
                "source_id": "S-001",
                "url": "https://history-a.example/meiji",
                "title": "明治維新",
                "language": "ja",
                "retrieved_at": "2026-09-21T00:00:00Z",
                "content_sha256": "a" * 64,
                "fetch_status": "fetched",
                "origin_key": "o-a",
                "source_kind": "secondary",
            }
        ],
        links=[link, dissent_link],
    )
    return parse_evidence_artifact(payload)


def _claim(cid: str, text: str, assessment: str, expr: str) -> dict[str, Any]:
    return {
        "claim_id": cid,
        "text": text,
        "kind": "year",
        "importance": "supporting",
        "strong": False,
        "assessed": True,
        "assessment": assessment,
        "assessment_reason": "test",
        "usable_expression": expr,
    }


def _request(
    *texts: str, evidence_id: str = REQUEST_ID, sha: str = "b" * 64
) -> ScriptVerificationRequest:
    return ScriptVerificationRequest.model_validate(
        {
            "evidence_request_id": evidence_id,
            "evidence_artifact": {"artifact_id": REQUEST_ID, "sha256": sha},
            "language": "ja",
            "units": [
                {"unit_id": f"scene:s{i}:narration", "text": text}
                for i, text in enumerate(texts, 1)
            ],
        }
    )


def _verdict(*texts: str, status=ResearchStatus.COMPLETED, extra=()) -> dict[str, Any]:
    return verify_script(
        _request(*texts), _evidence(), evidence_status=status, extra_candidates=extra
    )


# ------------------------------------------------------------------ 規則


def test_a_script_within_the_evidence_passes() -> None:
    result = _verdict("明治維新は1868年に江戸幕府から明治政府へ政権が移った変革とされる。", DISSENT)
    assert result["verdict"] == VerificationVerdict.PASSED.value, result
    assert {c["claim_id"] for u in result["units"] for c in u["checks"]} == {"C-001", "C-002"}


def test_an_overstated_sentence_fails() -> None:
    result = _verdict("明治維新ではすべての藩が1868年に必ず明治政府へ政権を移した。")
    assert result["verdict"] == VerificationVerdict.FAILED.value
    assert any(c["outcome"] == "overstated" for u in result["units"] for c in u["checks"])


def test_a_disputed_claim_stated_as_fact_fails() -> None:
    result = _verdict("鉄砲伝来は1543年である。")
    assert result["verdict"] == VerificationVerdict.FAILED.value
    assert any(c["outcome"] == "disputed_as_fact" for u in result["units"] for c in u["checks"])


def test_an_unregistered_claim_fails() -> None:
    result = _verdict("関ヶ原の戦いは1600年に起きた。")
    assert result["verdict"] == VerificationVerdict.FAILED.value
    assert result["unregistered"][0]["unit_id"] == "scene:s1:narration"


def test_relying_on_an_insufficient_claim_is_insufficient_not_passed() -> None:
    result = _verdict("長篠の戦いは鉄砲の数で勝敗が決まった。")
    assert result["verdict"] == VerificationVerdict.INSUFFICIENT.value


def test_evidence_that_did_not_complete_can_never_pass() -> None:
    for status in (ResearchStatus.PARTIAL, ResearchStatus.FAILED):
        result = _verdict(MEIJI, status=status)
        assert result["verdict"] == VerificationVerdict.INSUFFICIENT.value


def test_extractor_candidates_are_added_to_the_deterministic_net() -> None:
    plain = "明治の政府は新しかった。"
    assert _verdict(plain)["verdict"] == VerificationVerdict.PASSED.value  # 網は拾わない
    extra = (CandidateSentence("scene:s1:narration", plain, ("extractor",)),)
    assert _verdict(plain, extra=extra)["verdict"] == VerificationVerdict.FAILED.value


def test_the_evidence_must_belong_to_the_request() -> None:
    other = "0b0f0c46-7f6c-4c1e-9f55-0d7c0e0f9c11"
    with pytest.raises(ValueError, match="different research request"):
        verify_script(
            _request(MEIJI, evidence_id=other),
            _evidence(),
            evidence_status=ResearchStatus.COMPLETED,
        )


# ------------------------------------------------------------------ 保存（ScriptVerifier）


async def _completed_evidence(session_factory, store) -> tuple[str, Any]:
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    await evidence_executor(session_factory, store).execute(request_id)
    record, _ = await stored_evidence(session_factory, store, request_id)
    return request_id, record


def _svc_request(request_id: str, record: Any, *texts: str) -> ScriptVerificationRequest:
    return ScriptVerificationRequest.model_validate(
        {
            "evidence_request_id": request_id,
            "evidence_artifact": {"artifact_id": record.id, "sha256": record.sha256},
            "language": "ja",
            "units": [{"unit_id": "hook", "text": texts[0]}],
            "episode_id": "0b0f0c46-7f6c-4c1e-9f55-0d7c0e0f9c11",
        }
    )


async def test_the_verification_is_recorded_under_the_evidence_request(
    session_factory, artifact_store
) -> None:
    request_id, record = await _completed_evidence(session_factory, artifact_store)
    verifier = ScriptVerifier(
        session_factory=session_factory,
        store=artifact_store,
        bucket="artifacts",
        extractor=FakeClaimExtractor(),
    )
    outcome = await verifier.verify(
        _svc_request(request_id, record, "鉄砲伝来は1543年とされるが、1542年とする異説もある。")
    )
    assert outcome.verdict is VerificationVerdict.PASSED

    async with session_factory() as session:
        stored = await ResearchArtifactRepository(session).find_current(
            request_id, ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION
        )
        request = await ResearchRequestRepository(session).get(request_id)
    assert stored is not None and stored.id == outcome.artifact_ref.artifact_id
    assert request is not None and request.status is ResearchStatus.COMPLETED  # 状態は変えない
    body = parse_script_verification_artifact(await artifact_store.get_json(stored.object_key))
    assert body.request_id == request_id and body.episode_id is not None

    failed = await verifier.verify(_svc_request(request_id, record, "鉄砲伝来は1543年である。"))
    assert failed.verdict is VerificationVerdict.FAILED


async def test_partial_evidence_without_an_assessor_verifies_as_insufficient(
    session_factory, artifact_store
) -> None:
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    await evidence_executor(
        session_factory, artifact_store, evidence_providers(with_assessor=False)
    ).execute(request_id)
    record, _ = await stored_evidence(session_factory, artifact_store, request_id)
    verifier = ScriptVerifier(
        session_factory=session_factory, store=artifact_store, bucket="artifacts"
    )
    outcome = await verifier.verify(_svc_request(request_id, record, "鉄砲伝来は1543年とされる。"))
    assert outcome.verdict is VerificationVerdict.INSUFFICIENT


async def test_a_wrong_or_corrupted_evidence_artifact_is_not_verified(
    session_factory, artifact_store
) -> None:
    request_id, record = await _completed_evidence(session_factory, artifact_store)
    verifier = ScriptVerifier(
        session_factory=session_factory, store=artifact_store, bucket="artifacts"
    )
    wrong = ScriptVerificationRequest.model_validate(
        {
            **_svc_request(request_id, record, "x").model_dump(mode="json"),
            "evidence_artifact": {"artifact_id": record.id, "sha256": "c" * 64},
        }
    )
    with pytest.raises(ResearchInputInvalidError):
        await verifier.verify(wrong)

    class Corrupt(InMemoryArtifactStore):
        async def sha256_of(self, key: str) -> str:
            return "0" * 64

    corrupt = Corrupt()
    corrupt._objects = artifact_store._objects  # type: ignore[attr-defined]
    broken = ScriptVerifier(session_factory=session_factory, store=corrupt, bucket="artifacts")
    with pytest.raises(ResearchArtifactReadbackError):
        await broken.verify(_svc_request(request_id, record, "x"))
