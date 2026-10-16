"""DecisionRecord の導出と revision link（JP-MAS-001 §7、ADR-0041、records.py）。

候補・スコアは**保存済みの実データからだけ**作り、補完しないことを検査する。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select

from contracts.intervention import (
    AssessmentKind,
    EvidenceCheck,
    RationaleKind,
    RevisionLinkKind,
)
from contracts.states import ArtifactType
from contracts.topic_planning import AnalyticsMode, DuplicateLevel
from infrastructure.db.models import DecisionRecordRow, RevisionLinkRow
from infrastructure.db.repositories import (
    EpisodeRepository,
    NewTopicCandidate,
    NewTopicPlan,
    TopicPlanRepository,
)
from infrastructure.intervention.records import (
    check_evidence,
    derive_links,
    derive_topic_decision,
    generation_decision_payload,
    json_upstream_refs,
    record_derived_links,
    record_topic_decision,
)
from infrastructure.intervention.store import InterventionStore
from tests.support.storyboard import BUCKET, create_episode_at_script_ready, record_script


def _payload(topic: str, reason: str, audience: float, visual: float) -> dict[str, Any]:
    return {
        "topic": topic,
        "subject": topic.lower().replace(" ", "_"),
        "entities": [],
        "era": "edo",
        "theme": "craft",
        "angle": "reason",
        "hook": f"hook of {topic}",
        "visual_concept": "prints",
        "reason": reason,
        "audience_fit": audience,
        "visual_fit": visual,
    }


async def _plan(session, *, round_: int = 1, legacy_payload: bool = False) -> str:
    candidates = [
        NewTopicCandidate(
            ordinal=0,
            round=round_,
            payload={"topic": "t"} if legacy_payload else _payload("Ukiyoe", "visual", 0.8, 0.9),
            subject="ukiyoe",
            angle="reason",
            duplicate_level=DuplicateLevel.NONE,
            duplicate_score=0.1,
            score=0.71,
            score_breakdown={"novelty": 1.0, "production_fit": 0.9},
            rejected=False,
        ),
        NewTopicCandidate(
            ordinal=1,
            round=round_,
            payload=_payload("Samurai swords", "popular", 0.7, 0.6),
            subject="daisho",
            angle="reason",
            duplicate_level=DuplicateLevel.SEMANTIC,
            duplicate_score=0.92,
            duplicate_of="Why samurai wore two swords",
            score=None,
            score_breakdown={"novelty": 0.0},
            rejected=True,
        ),
        NewTopicCandidate(
            ordinal=2,
            round=round_,
            payload=_payload("Edo fire brigades", "drama", 0.5, 0.4),
            subject="hikeshi",
            angle="reason",
            duplicate_level=DuplicateLevel.NONE,
            duplicate_score=0.05,
            score=0.55,
            score_breakdown={"novelty": 1.0, "production_fit": 0.4},
            rejected=False,
        ),
    ]
    saved = await TopicPlanRepository(session).save_plan(
        NewTopicPlan(
            plan_date=date(2026, 10, 7),
            strategy_profile_id="jp_history_v1",
            strategy_version="1",
            content_profile_id="shorts",
            content_profile_version="1",
            topic="Ukiyoe",
            subject="ukiyoe",
            angle="reason",
            era="edo",
            theme="craft",
            hook="hook",
            entities=[],
            score=0.71,
            score_breakdown={"novelty": 1.0},
            duplicate_score=0.1,
            duplicate_level=DuplicateLevel.NONE,
            analytics_mode=AnalyticsMode.NO_ANALYTICS,
            analytics_confidence=0.0,
            planner_version="topic-planner-1",
            prompt_version="topic_en@2",
        ),
        candidates,
        selected_ordinal=0,
    )
    await session.commit()
    return saved.plan.id


async def test_choose_topic_uses_exactly_the_persisted_candidates(session) -> None:
    plan_id = await _plan(session)
    plan = await TopicPlanRepository(session).get(plan_id)
    rows = await TopicPlanRepository(session).list_candidates(plan_id)
    payload = await derive_topic_decision(session, plan_id)
    assert payload is not None and plan is not None
    assert payload.decision_type == "choose_topic"
    # 候補は保存された3件だけ（足さない・減らさない）
    assert [c.id for c in payload.candidates] == [r.id for r in rows]
    assert payload.selected_candidate == plan.selected_candidate_id
    # 選択理由はモデルの自己申告
    assert payload.rationale_kind is RationaleKind.REPORTED
    assert payload.rationale == "visual"
    chosen, duplicate, lower = payload.candidates
    # 規則の点数と自己評価を混ぜない
    assert chosen.assessment_kind is AssessmentKind.RULE
    assert chosen.scores == {"score": 0.71, "novelty": 1.0, "production_fit": 0.9}
    assert chosen.self_assessed_scores == {"audience_fit": 0.8, "visual_fit": 0.9}
    assert chosen.rejected_reason is None
    assert duplicate.scores == {"novelty": 0.0}  # 却下候補の点数は保存されていない → 足さない
    assert duplicate.rejected_reason is not None and "semantic" in duplicate.rejected_reason
    assert "Why samurai wore two swords" in duplicate.rejected_reason
    assert lower.rejected_reason is not None and "0.55" in lower.rejected_reason


async def test_candidates_from_earlier_rounds_are_declared_missing(session) -> None:
    plan_id = await _plan(session, round_=2)
    payload = await derive_topic_decision(session, plan_id)
    assert payload is not None
    assert any("round" in u for u in payload.uncertainties)


async def test_missing_reason_and_self_assessment_are_not_filled_in(session) -> None:
    plan_id = await _plan(session, legacy_payload=True)
    payload = await derive_topic_decision(session, plan_id)
    assert payload is not None
    assert payload.rationale_kind is RationaleKind.NONE
    assert payload.rationale is None
    assert payload.candidates[0].self_assessed_scores is None


async def test_unknown_plan_derives_nothing(session) -> None:
    assert await derive_topic_decision(session, "00000000-0000-0000-0000-000000000000") is None


async def test_recording_the_topic_decision_is_idempotent(session) -> None:
    plan_id = await _plan(session)
    first = await record_topic_decision(session, plan_id)
    again = await record_topic_decision(session, plan_id)
    await session.commit()
    assert first is not None and again is not None
    assert first.decision_id == again.decision_id
    assert first.topic_plan_id == plan_id
    assert await session.scalar(select(func.count()).select_from(DecisionRecordRow)) == 1


def test_evidence_check_is_against_the_snapshot_text() -> None:
    snapshot = "Evidence: research_artifact:abc and https://example.org/doc"
    assert check_evidence((), snapshot) is EvidenceCheck.NOT_CHECKED
    assert check_evidence(("research_artifact:abc",), snapshot) is EvidenceCheck.VERIFIED_IN_INPUT
    assert (
        check_evidence(("research_artifact:abc", "book-123"), snapshot)
        is EvidenceCheck.UNVERIFIED_REF
    )
    assert check_evidence(("x",), None) is EvidenceCheck.UNVERIFIED_REF


def test_generation_decision_has_one_candidate_and_no_invented_rationale() -> None:
    payload = generation_decision_payload(
        decision_type="write_script",
        question="Write the script",
        candidate_summary="title: x; 3 scenes",
        result_revision="rev-1",
        input_snapshot_ref="agent-runs/e/r/context.json",
        tool_invocation_refs=("provider_reservation:r-1",),
    )
    assert len(payload.candidates) == 1
    assert payload.selected_candidate == payload.candidates[0].id
    assert payload.rationale_kind is RationaleKind.NONE and payload.rationale is None
    assert payload.candidates[0].scores is None
    assert payload.evidence_check is EvidenceCheck.NOT_CHECKED
    assert payload.result_revision == "rev-1"
    assert payload.tool_invocation_refs == ("provider_reservation:r-1",)


def test_json_refs_are_found_at_any_depth() -> None:
    a, b, c = (
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    )
    payload = {
        "source_storyboard": {"artifact_id": a, "sha256": "0" * 64},
        "source_script": {"artifact_id": b, "sha256": "0" * 64},
        "scenes": [{"image": {"artifact_id": c, "sha256": "0" * 64}}],
        "not_a_ref": {"artifact_id": "not-a-uuid"},
    }
    assert json_upstream_refs(payload) == [a, b, c]


async def _storyboard_meta(session_factory, store, episode_id: str, script_id: str, sha: str):
    from domain.artifact.hashing import canonical_json_bytes, sha256_hex
    from infrastructure.db.repositories import ArtifactMetadataRepository

    payload = {
        "type": "storyboard",
        "source_script": {"artifact_id": script_id, "sha256": sha, "schema_version": "1.0"},
    }
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(f"episodes/{episode_id}/storyboard/{digest}.json", payload)
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version="1.0",
            bucket=BUCKET,
            object_key=put.key,
            sha256=put.sha256,
            input_hash="h" * 64,
        )
        await session.commit()
    return meta, payload


async def test_derived_links_are_written_once_for_existing_upstreams(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    async with session_factory() as session:
        from infrastructure.db.repositories import ArtifactMetadataRepository

        script = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCRIPT
        )
    assert script is not None
    meta, payload = await _storyboard_meta(
        session_factory, artifact_store, episode_id, script.id, script.sha256
    )
    payload_with_ghost = {
        **payload,
        "ghost": {"artifact_id": "99999999-9999-9999-9999-999999999999"},
    }
    async with session_factory() as session:
        written = await record_derived_links(session, episode_id, meta.id, payload_with_ghost)
        again = await record_derived_links(session, episode_id, meta.id, payload_with_ghost)
        await session.commit()
        links = await InterventionStore(session).links(episode_id=episode_id)
    assert written == [script.id]
    assert again == [script.id]
    assert [(lk.upstream_revision_id, lk.downstream_revision_id, lk.kind) for lk in links] == [
        (script.id, meta.id, RevisionLinkKind.DERIVED_FROM)
    ]


async def test_old_episodes_get_the_same_edges_by_reading_without_writing(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    async with session_factory() as session:
        from infrastructure.db.repositories import ArtifactMetadataRepository

        script = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCRIPT
        )
    assert script is not None
    meta, _ = await _storyboard_meta(
        session_factory, artifact_store, episode_id, script.id, script.sha256
    )
    async with session_factory() as session:
        derived = await derive_links(session, artifact_store, episode_id)
        count = await session.scalar(select(func.count()).select_from(RevisionLinkRow))
    assert [(e.upstream_revision_id, e.downstream_revision_id) for e in derived.edges] == [
        (script.id, meta.id)
    ]
    assert derived.unreadable == []
    assert count == 0


async def test_unreadable_artifacts_are_reported_not_skipped_silently(
    session_factory, artifact_store
) -> None:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    await record_script(session_factory, artifact_store, episode.id)
    from infrastructure.storage.memory_store import InMemoryArtifactStore

    async with session_factory() as session:
        derived = await derive_links(session, InMemoryArtifactStore(), episode.id)
    assert derived.edges == []
    assert len(derived.unreadable) == 1
