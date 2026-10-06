"""台本の Evidence 照合（opt-in）の Activity（ADR-0038 §B6、INV-37）。

守るもの:
- 台本の文面から主張の候補を拾い、**Gateway 経由で** Evidence を依頼し（``episode_id`` は
  research 側に参照として残る）、``completed`` なら照合して結果の参照を返す
  （照合結果は Evidence の依頼が所有）
- ``completed`` 以外（``partial`` / ``blocked``）・待つ上限の超過・主張なし・照合の例外は、どれも
  **例外にせず**「調査なし」相当の結果を返す（呼び出し側の台本工程は進む）
- 同じ台本の再実行は同じ依頼に戻る（冪等キー。依頼を増やさない）
- Strategy は ``audience_description`` を読むだけ

理由は docs/testing/research-opt-in-rationale.md。
"""

from __future__ import annotations

from contracts.research import ResearchArtifactType, ResearchKind, ResearchStatus
from contracts.research_evidence import parse_script_verification_artifact
from contracts.topic_planning import DEFAULT_STRATEGY_PROFILE_ID, STRATEGY_PROFILES
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.research.fake_evidence import FakeClaimExtractor
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from infrastructure.research.verification import ScriptVerifier
from tests.support.research_evidence import evidence_executor, evidence_providers
from tests.support.research_opt_in import (
    EPISODE_ID,
    FIXED_NOW,
    SCRIPT_ARTIFACT_ID,
    InlineResearchStarter,
    script_payload,
    store_script,
)
from workers.planning.script_evidence import EvidenceCheckOutcome, ScriptEvidenceRequest
from workers.planning.script_evidence_activities import (
    ScriptEvidenceActivities,
    claims_from_candidates,
    script_units,
)

FAKE = GatewayConfig(provider_mode="fake", provider_is_real=False, provider_configured=True)
NONE = GatewayConfig(provider_mode="none", provider_is_real=False, provider_configured=False)


async def _noop_sleep(_seconds: float) -> None:
    return None


def _activities(
    session_factory,
    store,
    *,
    config: GatewayConfig = FAKE,
    starter: InlineResearchStarter | None = None,
    verifier: ScriptVerifier | None = None,
    wait_seconds: float = 60,
) -> tuple[ScriptEvidenceActivities, InlineResearchStarter]:
    starter = starter or InlineResearchStarter(evidence_executor(session_factory, store))
    gateway = ResearchGateway(session_factory=session_factory, store=store, config=config)
    acts = ScriptEvidenceActivities(
        session_factory=session_factory,
        store=store,
        gateway=gateway,
        verifier=verifier
        or ScriptVerifier(session_factory=session_factory, store=store, bucket="artifacts"),
        starter=starter,
        channel_id="channel-1",
        default_strategy_profile_id=DEFAULT_STRATEGY_PROFILE_ID,
        extractor=FakeClaimExtractor(),
        clock=lambda: FIXED_NOW,
        wait_seconds=wait_seconds,
        poll_seconds=0,
        sleep=_noop_sleep,
        heartbeat=lambda: None,
    )
    return acts, starter


async def _request(store, *narrations: str) -> ScriptEvidenceRequest:
    key, sha = await store_script(store, script_payload(*narrations))
    return ScriptEvidenceRequest(
        episode_id=EPISODE_ID, artifact_id=SCRIPT_ARTIFACT_ID, object_key=key, sha256=sha
    )


async def _research(session_factory, request_id: str | None):
    assert request_id is not None
    async with session_factory() as session:
        return await ResearchRequestRepository(session).get(request_id)


async def test_completed_evidence_is_verified_and_recorded_under_the_evidence_request(
    session_factory, artifact_store
) -> None:
    acts, starter = _activities(session_factory, artifact_store)
    outcome = await acts.check(await _request(artifact_store))

    assert outcome.outcome == EvidenceCheckOutcome.VERIFIED
    assert outcome.research_status == ResearchStatus.COMPLETED
    assert outcome.verdict in {"passed", "failed", "insufficient"}
    assert starter.started == [outcome.research_request_id]

    research = await _research(session_factory, outcome.research_request_id)
    assert research is not None and research.kind is ResearchKind.EVIDENCE
    assert research.episode_id == EPISODE_ID  # 追跡は research 側の参照（本番の表に列を足さない）
    assert research.requester == "script_writer"
    audience = STRATEGY_PROFILES[DEFAULT_STRATEGY_PROFILE_ID].audience_description[:200]
    assert research.payload["audience"] == audience  # Strategy は読むだけ
    async with session_factory() as session:
        stored = await ResearchArtifactRepository(session).find_current(
            research.id, ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION
        )
    assert stored is not None and stored.id == outcome.verification_artifact_id
    body = parse_script_verification_artifact(await artifact_store.get_json(stored.object_key))
    assert body.episode_id == EPISODE_ID and body.verdict.value == outcome.verdict


async def test_rerunning_the_check_for_the_same_script_reuses_the_same_request(
    session_factory, artifact_store
) -> None:
    acts, starter = _activities(session_factory, artifact_store)
    request = await _request(artifact_store)
    first = await acts.check(request)
    second = await acts.check(request)
    assert first.research_request_id == second.research_request_id
    assert starter.started == [first.research_request_id]  # 2 回目は完了済みなので起動しない


async def test_partial_evidence_is_treated_as_no_research(session_factory, artifact_store) -> None:
    executor = evidence_executor(
        session_factory, artifact_store, evidence_providers(with_assessor=False)
    )
    acts, _ = _activities(session_factory, artifact_store, starter=InlineResearchStarter(executor))
    outcome = await acts.check(await _request(artifact_store))
    assert outcome.outcome == EvidenceCheckOutcome.NO_RESEARCH
    assert outcome.research_status == ResearchStatus.PARTIAL
    assert outcome.verdict is None and outcome.verification_artifact_id is None


async def test_a_blocked_request_is_not_started_and_is_treated_as_no_research(
    session_factory, artifact_store
) -> None:
    acts, starter = _activities(session_factory, artifact_store, config=NONE)
    outcome = await acts.check(await _request(artifact_store))
    assert outcome.outcome == EvidenceCheckOutcome.NO_RESEARCH
    assert outcome.research_status == ResearchStatus.BLOCKED
    assert outcome.reason is not None
    assert starter.started == []


async def test_research_that_does_not_finish_in_time_is_a_timeout(
    session_factory, artifact_store
) -> None:
    acts, starter = _activities(
        session_factory,
        artifact_store,
        starter=InlineResearchStarter(None, run=False),
        wait_seconds=0,
    )
    outcome = await acts.check(await _request(artifact_store))
    assert outcome.outcome == EvidenceCheckOutcome.TIMEOUT
    assert starter.started == [outcome.research_request_id]
    research = await _research(session_factory, outcome.research_request_id)
    assert research is not None and research.status is ResearchStatus.QUEUED  # 止めない


async def test_a_script_without_checkable_claims_submits_nothing(
    session_factory, artifact_store
) -> None:
    acts, starter = _activities(session_factory, artifact_store)
    outcome = await acts.check(
        await _request(artifact_store, "人々は驚いた", "船が来た", "島は静かだった")
    )
    assert outcome.outcome == EvidenceCheckOutcome.NO_CLAIMS
    assert outcome.research_request_id is None and starter.started == []


async def test_a_verification_failure_is_reported_not_raised(
    session_factory, artifact_store
) -> None:
    class Broken(ScriptVerifier):
        async def verify(self, request):  # type: ignore[override]
            raise RuntimeError("verifier down")

    acts, _ = _activities(
        session_factory,
        artifact_store,
        verifier=Broken(session_factory=session_factory, store=artifact_store, bucket="b"),
    )
    outcome = await acts.check(await _request(artifact_store))
    assert outcome.outcome == EvidenceCheckOutcome.ERROR
    assert outcome.reason == "RuntimeError"  # 型名だけ（例外文は写さない / INV-20）


async def test_a_script_that_does_not_match_its_sha256_is_not_used(
    session_factory, artifact_store
) -> None:
    acts, starter = _activities(session_factory, artifact_store)
    request = await _request(artifact_store)
    request.sha256 = "0" * 64
    outcome = await acts.check(request)
    assert outcome.outcome == EvidenceCheckOutcome.ERROR and starter.started == []


def test_claims_come_from_the_final_script_text_with_title_and_hook_as_central() -> None:
    from contracts.artifacts import parse_script_artifact
    from domain.research.script_verification import detect_candidates

    script = parse_script_artifact(script_payload("1543年に鉄砲が伝わった。", "1868年に変わった。"))
    units = script_units(script)
    assert units[:2] == [("title", script.title), ("hook", script.hook)]
    assert ("scene:s1:narration", "1543年に鉄砲が伝わった。") in units
    claims = claims_from_candidates(detect_candidates(units) * 2)  # 重複は 1 件にする
    assert [c.claim_text for c in claims] == ["1543年に鉄砲が伝わった。", "1868年に変わった。"]
    assert {c.kind.value for c in claims} == {"year"}
