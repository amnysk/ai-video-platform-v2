"""独立レビュアー（RoleSpec REVIEWER、JP-MAS-001 §3/§17、reviewer.py）。

決定論の検査だけ。LLM を呼ばず、成果物の revision を作らず、所見は採用 manifest に結び付く。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from contracts.artifacts import build_storyboard_artifact
from contracts.intervention import ActorType, AnnotationKind, InterventionMode
from contracts.roles import REVIEWER
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.db.models import (
    AgentRunRow,
    AnnotationRow,
    ArtifactMetadataRow,
    DecisionRecordRow,
    ResearchArtifactRow,
    ResearchRequestRow,
)
from infrastructure.intervention.reviewer import REVIEW_VERDICT, run_reviewer
from infrastructure.intervention.store import ControlChange, InterventionStore
from tests.support.storyboard import create_episode_at_script_ready
from tests.unit.test_intervention_read_model import HUMAN, _put, _script

FITS = "縄文土器には焦げ跡が残る。"


def _valid_storyboard(episode_id: str, script_meta, *, s1_ms: int = 8000) -> dict:
    spans = (("s1", s1_ms), ("s2", 17000 - s1_ms), ("s3", 8000))
    scenes, start = [], 0
    for order, (sid, ms) in enumerate(spans, 1):
        scenes.append(
            {
                "scene_id": f"sb{order}",
                "order": order,
                "script_scene_id": sid,
                "start_ms": start,
                "duration_ms": ms,
                "visual_kind": "broll",
                "visual_description": sid,
            }
        )
        start += ms
    return build_storyboard_artifact(
        episode_id=episode_id,
        source_script={
            "artifact_id": script_meta.id,
            "sha256": script_meta.sha256,
            "schema_version": "1.0",
        },
        scenes=scenes,
        total_duration_ms=25000,
        metadata={"generator": "fake", "generator_model": "m", "generation_spec_id": "s"},
    )


async def _episode(session_factory, store, *, narration_s1: str = FITS, s1_ms: int = 8000):
    episode_id = await create_episode_at_script_ready(session_factory, store, with_script=False)
    script = await _put(
        session_factory,
        store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1=narration_s1),
    )
    storyboard = await _put(
        session_factory,
        store,
        episode_id,
        ArtifactType.STORYBOARD,
        _valid_storyboard(episode_id, script, s1_ms=s1_ms),
    )
    async with session_factory() as session:
        control = InterventionStore(session)
        await control.lock_control(episode_id)
        await control.update_control(
            episode_id, ControlChange(mode=InterventionMode.CO_PRODUCTION, bump_revision=False)
        )
        await session.commit()
    return episode_id, script, storyboard


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _checks(result) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in result.findings:
        out.setdefault(f.check, []).append(f.severity)
    return out


async def test_a_clean_manifest_passes_and_is_recorded_without_new_revisions(
    session_factory, artifact_store
) -> None:
    episode_id, script, storyboard = await _episode(session_factory, artifact_store)
    revisions_before = await _count(session_factory, ArtifactMetadataRow)

    result = await run_reviewer(session_factory, artifact_store, episode_id)

    assert REVIEWER.may_create_revisions is False
    assert await _count(session_factory, ArtifactMetadataRow) == revisions_before
    assert result.verdict == "pass"
    assert all(f.severity == "advisory" for f in result.findings)
    assert {"final_video", "evidence"} <= set(_checks(result))  # 未生成・未照合は助言
    async with session_factory() as session:
        store = InterventionStore(session)
        control = await store.get_control(episode_id)
        run = await store.get_agent_run(result.agent_run_id)
        (decision,) = await store.list_decisions(episode_id=episode_id)
        findings = await store.list_annotations(episode_id, kind=AnnotationKind.REVIEW_FINDING)
    assert run is not None and run.role.value == "reviewer" and run.output_revision_id is None
    assert run.model is None and run.token_usage is None
    snapshot = json.loads(await artifact_store.get_bytes(run.snapshot_object_key))
    assert snapshot["prompt_source"] == "not_applicable"
    members = snapshot["input_refs"]["members"]
    assert members["script"] == {"revision_id": script.id, "sha256": script.sha256}
    assert members["storyboard"]["revision_id"] == storyboard.id
    assert result.manifest_id == control.active_manifest_id
    assert decision.decision_type == REVIEW_VERDICT
    assert decision.agent_run_id == result.agent_run_id
    assert decision.payload["rationale_kind"] == "rule"
    assert decision.payload["selected_candidate"] == "pass"
    assert decision.payload["input_snapshot_ref"] == run.snapshot_object_key
    assert len(findings) == len(result.findings)
    assert all(
        f.manifest_id == result.manifest_id
        and f.agent_run_id == result.agent_run_id
        and f.actor.type is ActorType.AGENT
        for f in findings
    )


async def test_narration_that_does_not_fit_its_scene_is_blocking(
    session_factory, artifact_store
) -> None:
    episode_id, script, _ = await _episode(session_factory, artifact_store, narration_s1="長" * 100)
    result = await run_reviewer(session_factory, artifact_store, episode_id)
    assert result.verdict == "needs_attention"
    (finding,) = [f for f in result.findings if f.check == "narration_budget"]
    assert finding.severity == "blocking"
    assert finding.anchor["segment_id"] == "s1" and finding.anchor["revision_id"] == script.id


async def test_a_storyboard_span_too_short_for_its_narration_is_blocking(
    session_factory, artifact_store
) -> None:
    episode_id, _, storyboard = await _episode(session_factory, artifact_store, s1_ms=1000)
    result = await run_reviewer(session_factory, artifact_store, episode_id)
    (finding,) = [f for f in result.findings if f.check == "storyboard_span"]
    assert finding.severity == "blocking"
    assert finding.anchor["revision_id"] == storyboard.id


async def _final_video(session_factory, store, episode_id, manifest_id: str, *, passed=True):
    return await _put(
        session_factory,
        store,
        episode_id,
        ArtifactType.FINAL_VIDEO,
        {
            "type": "final_video",
            "source_production_manifest": {"artifact_id": manifest_id, "sha256": "0" * 64},
            "technical_qa": {"passed": passed, "checks": [{"check": "duration", "passed": True}]},
        },
    )


async def test_final_video_qa_and_its_production_manifest_are_checked(
    session_factory, artifact_store
) -> None:
    episode_id, _, _ = await _episode(session_factory, artifact_store)
    adopted = await _put(
        session_factory, artifact_store, episode_id, ArtifactType.PRODUCTION_MANIFEST, {"v": 1}
    )
    other = str(uuid.uuid4())
    await _final_video(session_factory, artifact_store, episode_id, other)
    result = await run_reviewer(session_factory, artifact_store, episode_id)
    assert "blocking" in _checks(result)["final_video"]
    assert result.verdict == "needs_attention"

    await _final_video(session_factory, artifact_store, episode_id, adopted.id)
    result = await run_reviewer(session_factory, artifact_store, episode_id)
    assert "final_video" not in _checks(result)


async def test_open_comments_stale_approvals_and_failed_verification_are_reported(
    session_factory, artifact_store
) -> None:
    episode_id, script, _ = await _episode(session_factory, artifact_store)
    first = await run_reviewer(session_factory, artifact_store, episode_id)
    async with session_factory() as session:
        store = InterventionStore(session)
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": script.id, "segment_id": "s2"},
            body="ここを確認したい",
            actor=HUMAN,
        )
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.APPROVAL,
            anchor={"manifest_id": str(uuid.uuid4())},
            body="old ok",
            actor=HUMAN,
            manifest_id=str(uuid.uuid4()),  # 採用中でない manifest への承認
        )
        request_id = uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            ResearchRequestRow(
                id=request_id,
                idempotency_key="rk",
                request_hash="0" * 64,
                kind="evidence",
                status="completed",
                requester="t",
                channel_id="c",
                episode_id=uuid.UUID(episode_id),
                payload={},
                limits={},
                policy_version="p",
                schema_version="1.0",
                provider_config_version="v",
                as_of=now,
            )
        )
        await session.commit()
    body = {"verdict": "failed", "script_ref": {"artifact_id": script.id, "sha256": script.sha256}}
    digest = sha256_hex(canonical_json_bytes(body))
    put = await artifact_store.put_json(f"research/{request_id}/v/{digest}.json", body)
    async with session_factory() as session:
        session.add(
            ResearchArtifactRow(
                request_id=request_id,
                artifact_type="research_script_verification",
                schema_version="1.0",
                bucket="artifacts",
                object_key=put.key,
                sha256=put.sha256,
                size_bytes=put.size,
                version=1,
            )
        )
        await session.commit()

    result = await run_reviewer(session_factory, artifact_store, episode_id)

    checks = _checks(result)
    assert checks["open_comments"] == ["advisory"]
    assert checks["stale_approval"] == ["advisory"]
    assert checks["evidence"] == ["advisory"]
    (evidence,) = [f for f in result.findings if f.check == "evidence"]
    assert "failed" in evidence.message
    # 前回のレビュアー所見は「人のコメント」として数えない
    assert first.agent_run_id != result.agent_run_id


async def test_rerunning_on_the_same_inputs_does_not_add_records(
    session_factory, artifact_store
) -> None:
    episode_id, _, _ = await _episode(session_factory, artifact_store)
    first = await run_reviewer(session_factory, artifact_store, episode_id)
    counts = [
        await _count(session_factory, m) for m in (AgentRunRow, AnnotationRow, DecisionRecordRow)
    ]
    again = await run_reviewer(session_factory, artifact_store, episode_id)
    assert again.reused is True
    assert again.agent_run_id == first.agent_run_id
    assert again.decision_id == first.decision_id
    assert [f.check for f in again.findings] == [f.check for f in first.findings]
    assert [
        await _count(session_factory, m) for m in (AgentRunRow, AnnotationRow, DecisionRecordRow)
    ] == counts
