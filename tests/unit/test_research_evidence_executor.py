"""Evidence Research を実行器で走らせる（ADR-0038）。SQLite・インメモリの ArtifactStore・Fake。

守るもの:
- 評価器の呼び出しは台帳の ``assess`` 行を通る（予約 → dispatch → spent）。枠 ``max_assessments``
  を越えない（INV-36）。再実行は保存済みの提案を読み、評価器を呼び直さない
- 評価器が無い・枠が尽きた claim は ``insufficient`` のまま、依頼は ``partial``（合格にしない）
- 資料の URL は取得した ``final_url`` だけ。切り詰め・失敗した取得は根拠にならない
- 悪い提案（作った抜粋・取得していない URL・存在しない資料・強すぎる表現）は採用されない
- Provider ``none`` は外部を呼ばずに ``blocked``。鮮度内の Evidence は Gateway が検証して再利用する

理由は docs/testing/research-evidence-rationale.md。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from contracts.research import (
    ResearchCall,
    ResearchCallStatus,
    ResearchStatus,
    parse_research_spec,
    parse_research_submit,
)
from contracts.research_evidence import (
    ClaimAssessment,
    SourceFetchStatus,
    Stance,
)
from domain.research.errors import ResearchOutputInvalidError
from domain.research.evidence_handler import EvidenceHandler
from domain.research.handlers import FetchedSource, FetchTarget, HandlerOutput, SynthesisContext
from domain.research.ports import SearchHit
from infrastructure.db.models import ResearchCallRow
from infrastructure.db.research_repositories import ResearchCallRepository
from infrastructure.research.fake_corpus import (
    SEKIGAHARA_A,
    SEKIGAHARA_SHORT_LINK,
    TRUNCATED_URL,
)
from infrastructure.research.fake_evidence import BadProposal, FakeEvidenceAssessor
from infrastructure.research.fake_providers import FakeContentFetcher
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from infrastructure.research.registry import ResearchProviders
from tests.support.research import AS_OF
from tests.support.research_evidence import (
    MEIJI_CLAIM,
    TANEGASHIMA_CLAIM,
    claims_payload,
    evidence_executor,
    evidence_providers,
    make_request,
    stored_evidence,
)

REQUEST_ID = "7a6c119d-bc25-5ce8-aa48-a0901a9844ae"


async def _calls(session_factory, request_id: str):
    async with session_factory() as session:
        return await ResearchCallRepository(session).list_for_request(request_id)


# ------------------------------------------------------------------ 正常系


async def test_evidence_runs_search_fetch_and_assess_through_the_ledger(
    session_factory, artifact_store
) -> None:
    assessor = FakeEvidenceAssessor()
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    outcome = await evidence_executor(
        session_factory, artifact_store, evidence_providers(assessor=assessor)
    ).execute(request_id)

    assert outcome.status is ResearchStatus.COMPLETED
    calls = await _calls(session_factory, request_id)
    assess = [c for c in calls if c.call is ResearchCall.ASSESS]
    assert len(assess) == len(assessor.calls) == outcome.usage.assessments == 1
    assert all(c.status is ResearchCallStatus.SPENT and c.dispatched_at for c in calls)

    _, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    claim = artifact.claims[0]
    assert claim.assessed and claim.assessment is ClaimAssessment.DISPUTED  # 1542 年の異説
    assert any(link.stance is Stance.REFUTES for link in artifact.links)


async def test_a_rerun_reads_the_saved_proposal_and_does_not_call_the_assessor_again(
    session_factory, artifact_store
) -> None:
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    first = FakeEvidenceAssessor()
    executor = evidence_executor(
        session_factory, artifact_store, evidence_providers(assessor=first)
    )
    # 呼び出しの後・成果物の前に落ちたことにする: 実行器の「確定」だけを失敗させる
    original = executor._finalize

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after the calls")

    executor._finalize = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await executor.execute(request_id)
    executor._finalize = original  # type: ignore[method-assign]

    second = FakeEvidenceAssessor()
    rerun = evidence_executor(session_factory, artifact_store, evidence_providers(assessor=second))
    outcome = await rerun.execute(request_id)
    assert outcome.status is ResearchStatus.COMPLETED
    assert len(first.calls) == 1 and second.calls == []
    assert (
        len([c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS])
        == 1
    )


# ------------------------------------------------------------------ fail-closed


async def test_without_an_assessor_claims_stay_unassessed_and_the_request_is_partial(
    session_factory, artifact_store
) -> None:
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    outcome = await evidence_executor(
        session_factory, artifact_store, evidence_providers(with_assessor=False)
    ).execute(request_id)

    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "assessor_not_available"
    assert not [
        c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS
    ]
    _, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    assert all(
        not c.assessed and c.assessment is ClaimAssessment.INSUFFICIENT for c in artifact.claims
    )
    assert artifact.links == ()


async def test_assessments_beyond_the_ceiling_are_not_sent_and_the_request_is_partial(
    session_factory, artifact_store
) -> None:
    payload = claims_payload(
        TANEGASHIMA_CLAIM, MEIJI_CLAIM, limits={"max_assessments": 1, "max_searches": 5}
    )
    request_id = await make_request(session_factory, payload)
    assessor = FakeEvidenceAssessor()
    outcome = await evidence_executor(
        session_factory, artifact_store, evidence_providers(assessor=assessor)
    ).execute(request_id)

    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "call_budget_exhausted"
    assess = [c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.ASSESS]
    assert len(assess) == len(assessor.calls) == 1  # INV-36
    _, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    unassessed = [c for c in artifact.claims if not c.assessed]
    assert len(unassessed) == 1 and unassessed[0].assessment is ClaimAssessment.INSUFFICIENT


async def test_provider_none_is_blocked_without_any_call(session_factory, artifact_store) -> None:
    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    none = ResearchProviders(mode="none", search=None, fetcher=None, is_real=False)
    outcome = await evidence_executor(session_factory, artifact_store, none).execute(request_id)
    assert outcome.status is ResearchStatus.BLOCKED
    assert outcome.stop_code == "provider_not_configured"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ResearchCallRow)) == 0


@pytest.mark.parametrize("bad", list(BadProposal))
async def test_a_bad_proposal_never_becomes_supported(session_factory, artifact_store, bad) -> None:
    request_id = await make_request(session_factory, claims_payload(MEIJI_CLAIM))
    await evidence_executor(
        session_factory, artifact_store, evidence_providers(assessor=FakeEvidenceAssessor(bad=bad))
    ).execute(request_id)
    _, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    claim = artifact.claims[0]
    if bad is BadProposal.OVERSTATED:
        # 裏付けは本物だが、強すぎる表現は採らず「資料によれば」の形に置き換える
        assert "常に" not in claim.usable_expression
        assert claim.usable_expression.startswith("資料によれば")
    else:
        assert claim.assessment is not ClaimAssessment.SUPPORTED
        assert "made-up" not in str(artifact.model_dump())


async def test_an_assessor_output_that_violates_the_schema_is_a_hole_not_a_pass(
    session_factory, artifact_store
) -> None:
    class Broken(FakeEvidenceAssessor):
        async def assess(self, claim, passages):  # type: ignore[override]
            self.calls.append((claim, tuple(passages)))
            return {"claim_id": claim.claim_id, "assessment": "supported"}  # 欠けた出力

    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    outcome = await evidence_executor(
        session_factory,
        artifact_store,
        evidence_providers(assessor=Broken()),  # pyright: ignore[reportArgumentType]
    ).execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    _, artifact = await stored_evidence(session_factory, artifact_store, request_id)
    assert artifact.claims[0].assessment is ClaimAssessment.INSUFFICIENT


# ------------------------------------------------------------------ 資料


async def test_sources_use_the_fetched_final_url_and_unconfirmed_bodies_never_back_a_claim() -> (
    None
):
    claim = {"claim_text": "関ヶ原の戦いは1600年", "kind": "year", "importance": "supporting"}
    spec = parse_research_spec(claims_payload(claim))
    fetcher = FakeContentFetcher()
    fetched = [
        FetchedSource(target=_target(url), content=await fetcher.fetch(url))
        for url in (SEKIGAHARA_SHORT_LINK, TRUNCATED_URL)
    ]
    handler = EvidenceHandler()
    tasks = handler.plan_assessments(spec, (), fetched)
    # 評価器へ渡す passage は本文を完全に確認した資料からだけ
    assert tasks and all(p.source_url == SEKIGAHARA_A for t in tasks for p in t.passages)

    output = handler.synthesize(
        spec, SynthesisContext(request_id=REQUEST_ID, as_of=AS_OF), (), fetched
    )
    sources = {s["url"]: s for s in output.artifact["sources"]}
    # 転送先の final_url だけが載り、短縮 URL（検索結果の URL）は載らない
    assert set(sources) == {SEKIGAHARA_A, TRUNCATED_URL}
    assert sources[TRUNCATED_URL]["fetch_status"] == SourceFetchStatus.TRUNCATED.value
    assert sources[TRUNCATED_URL]["content_sha256"] is not None
    assert not output.complete  # 評価していない claim がある


def _target(url: str) -> FetchTarget:
    hit = SearchHit(url=url, title="関ヶ原", snippet="s", published_at=None, provider="fake")
    return FetchTarget(hit=hit, step_id="c001-primary")


async def test_the_executor_rejects_evidence_sources_that_were_not_fetched(
    session_factory, artifact_store
) -> None:
    class Forging(EvidenceHandler):
        def synthesize(self, spec, ctx, rounds, fetched):  # type: ignore[override]
            output = super().synthesize(spec, ctx, rounds, fetched)
            artifact = dict(output.artifact)
            artifact["sources"] = [
                {**s, "url": "https://invented.example/by-the-llm"} for s in artifact["sources"]
            ]
            return HandlerOutput(
                artifact_type=output.artifact_type,
                schema_version=output.schema_version,
                artifact=artifact,
                complete=output.complete,
                coverage=output.coverage,
            )

    request_id = await make_request(session_factory, claims_payload(TANEGASHIMA_CLAIM))
    executor = evidence_executor(session_factory, artifact_store)
    executor._handlers = {Forging().kind: Forging()}  # type: ignore[attr-defined]
    with pytest.raises(ResearchOutputInvalidError):
        await executor.execute(request_id)


# ------------------------------------------------------------------ 鮮度（Gateway の再利用）


async def test_a_fresh_completed_evidence_request_is_reused_after_verification(
    session_factory, artifact_store
) -> None:
    config = GatewayConfig(provider_mode="fake", provider_is_real=False, provider_configured=True)
    gateway = ResearchGateway(session_factory=session_factory, store=artifact_store, config=config)
    first = await gateway.submit(
        parse_research_submit({**claims_payload(TANEGASHIMA_CLAIM), "idempotency_key": "ev:1"})
    )
    await evidence_executor(session_factory, artifact_store).execute(first.request.id)

    again = await gateway.submit(
        parse_research_submit({**claims_payload(TANEGASHIMA_CLAIM), "idempotency_key": "ev:2"})
    )
    assert again.reused and again.request.id == first.request.id

    later = ResearchGateway(
        session_factory=session_factory,
        store=artifact_store,
        config=config,
        clock=lambda: datetime.now(UTC) + timedelta(days=config.evidence_reverify_days + 1),
    )
    stale = await later.submit(
        parse_research_submit({**claims_payload(TANEGASHIMA_CLAIM), "idempotency_key": "ev:3"})
    )
    assert not stale.reused and stale.request.id != first.request.id
