"""独立レビュアー（RoleSpec ``REVIEWER``、JP-MAS-001 §3 / §17、ADR-0041）。

採用 manifest を**自分の入力 snapshot**（member の revision と sha256、考慮した注記・照合）で、
決定論の規則だけを使って検査する。LLM は呼ばない（``max_llm_calls_per_run=0``）。
成果物の revision を作らず（``may_create_revisions=False``）、出すのは
所見 ``annotations(kind=review_finding)``（採用 manifest に結び付く）・判定
``decision_records(review_verdict)`` 1件（``rationale_kind=rule``）・実行記録 ``agent_runs``。

検査（``REVIEWER_RULES_VERSION``。表は ``docs/domain/review-packet.md`` §独立レビュアー）:
blocking = integrity / stale_member / narration_budget / storyboard_span /
final_video（QA・manifest）、
advisory = final_video 未生成 / evidence / open_comments / stale_approval（INV-45）。

同じ manifest・同じ入力での再実行は、同じ記録を返す（増やさない）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifacts import parse_script_artifact, parse_storyboard_artifact
from contracts.intervention import (
    ActorType,
    AgentRole,
    AnnotationKind,
    AssessmentKind,
    DecisionCandidate,
    DecisionRecordPayload,
    InterventionEvent,
    RationaleKind,
)
from contracts.research import ResearchArtifactType
from contracts.topic_planning import script_locale_for_language
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import NeedsInputError
from domain.storyboard.coverage import check_storyboard_fits_narration
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.capture import (
    AgentRunRecorder,
    PromptSource,
    RunCapture,
    agent_run_id_for,
)
from infrastructure.intervention.manifests import ensure_manifest
from infrastructure.intervention.read_model import EpisodeReadModel, EpisodeView, MemberView
from infrastructure.intervention.store import SYSTEM_ACTOR, Actor, InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore

REVIEW_VERDICT = "review_verdict"
REVIEW_STEP = "review_manifest"
REVIEWER_RULES_VERSION = "reviewer-rules-1"
REVIEWER_ACTOR = Actor(ActorType.AGENT, AgentRole.REVIEWER.value)

BLOCKING = "blocking"
ADVISORY = "advisory"
VERDICT_PASS = "pass"
VERDICT_NEEDS_ATTENTION = "needs_attention"


class ReviewerConflictError(NeedsInputError):
    """検査の間に採用 manifest が替わった。所見を古い manifest に付けない（再実行する）。"""


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    severity: str
    message: str
    anchor: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "anchor": self.anchor,
        }


@dataclass(frozen=True, slots=True)
class ReviewResult:
    agent_run_id: str
    decision_id: str
    manifest_id: str
    verdict: str
    findings: list[Finding]
    annotation_ids: list[str]
    #: True = 同じ入力の既存の記録を返した（何も書いていない）
    reused: bool


# --------------------------------------------------------------------------- 検査


async def _load_member(
    store: ArtifactStore, member: MemberView | None, findings: list[Finding], manifest_id: str
) -> dict[str, Any] | None:
    if member is None or member.object_key is None:
        return None
    anchor = {"manifest_id": manifest_id, "revision_id": member.revision_id}
    try:
        body = await store.get_json(member.object_key)
    except Exception as exc:  # noqa: BLE001 - 読めないことを所見にする
        findings.append(
            Finding(
                "integrity",
                BLOCKING,
                f"{member.slot}: body unreadable ({type(exc).__name__})",
                anchor,
            )
        )
        return None
    if member.sha256 is not None and sha256_hex(canonical_json_bytes(body)) != member.sha256:
        findings.append(
            Finding("integrity", BLOCKING, f"{member.slot}: sha256 differs from the record", anchor)
        )
        return None
    return body


async def _inspect(view: EpisodeView, store: ArtifactStore, manifest_id: str) -> list[Finding]:
    findings: list[Finding] = []
    members = {m.slot: m for m in view.manifest.members}

    for member in view.manifest.members:
        for reason in member.stale_reasons:
            findings.append(
                Finding(
                    "stale_member",
                    BLOCKING,
                    f"{member.slot}: {reason}",
                    {"manifest_id": manifest_id, "revision_id": member.revision_id},
                )
            )

    script_member = members.get("script")
    script_body = await _load_member(store, script_member, findings, manifest_id)
    script = None
    if script_body is not None and script_member is not None:
        try:
            script = parse_script_artifact(script_body)
            locale = script_locale_for_language(script.language)
        except Exception as exc:  # noqa: BLE001 - 契約違反は所見
            findings.append(
                Finding(
                    "integrity",
                    BLOCKING,
                    f"script: contract violation ({type(exc).__name__})",
                    {"manifest_id": manifest_id, "revision_id": script_member.revision_id},
                )
            )
            script = None
        else:
            for scene in script.scenes:
                units = locale.count_speech_units(scene.narration)
                budget = locale.narration_budget(scene.duration_ms)
                if units > budget:
                    findings.append(
                        Finding(
                            "narration_budget",
                            BLOCKING,
                            f"{scene.id}: {units} {locale.speech_unit.value}s exceed the budget "
                            f"{budget} for {scene.duration_ms} ms ({locale.locale})",
                            {
                                "manifest_id": manifest_id,
                                "revision_id": script_member.revision_id,
                                "segment_id": scene.id,
                            },
                        )
                    )

    storyboard_member = members.get("storyboard")
    storyboard_body = await _load_member(store, storyboard_member, findings, manifest_id)
    if (
        storyboard_body is not None
        and storyboard_member is not None
        and script is not None
        and not storyboard_member.stale
    ):
        anchor = {"manifest_id": manifest_id, "revision_id": storyboard_member.revision_id}
        try:
            storyboard = parse_storyboard_artifact(storyboard_body)
            check_storyboard_fits_narration(
                storyboard, script, script_locale_for_language(script.language)
            )
        except Exception as exc:  # noqa: BLE001 - 規則違反・契約違反は所見
            findings.append(Finding("storyboard_span", BLOCKING, str(exc)[:500], anchor))

    final_member = members.get("final_video")
    final_body = await _load_member(store, final_member, findings, manifest_id)
    if final_member is None:
        findings.append(
            Finding(
                "final_video",
                ADVISORY,
                "no final video in the adopted manifest yet",
                {"manifest_id": manifest_id},
            )
        )
    elif final_body is not None:
        anchor = {"manifest_id": manifest_id, "revision_id": final_member.revision_id}
        qa = final_body.get("technical_qa")
        if not isinstance(qa, dict) or qa.get("passed") is not True or not qa.get("checks"):
            findings.append(
                Finding("final_video", BLOCKING, "technical_qa is missing or not passed", anchor)
            )
        source = (final_body.get("source_production_manifest") or {}).get("artifact_id")
        adopted = members.get("production_manifest")
        adopted_id = None if adopted is None else adopted.revision_id
        if source != adopted_id:
            findings.append(
                Finding(
                    "final_video",
                    BLOCKING,
                    f"rendered from production manifest {source}, adopted is {adopted_id}",
                    anchor,
                )
            )

    verifications = [
        e
        for e in view.evidence
        if e.artifact_type == ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION.value
        and e.is_current
    ]
    script_id = None if script_member is None else script_member.revision_id
    matching = [e for e in verifications if e.script_revision_id == script_id]
    evidence_anchor = {"manifest_id": manifest_id, "revision_id": script_id}
    if not verifications:
        findings.append(
            Finding(
                "evidence", ADVISORY, "no script verification for this episode", evidence_anchor
            )
        )
    elif not matching:
        findings.append(
            Finding(
                "evidence",
                ADVISORY,
                "the script verification is for another script revision",
                evidence_anchor,
            )
        )
    else:
        for e in matching:
            if e.verdict != "passed":
                findings.append(
                    Finding(
                        "evidence",
                        ADVISORY,
                        f"script verification {e.research_artifact_id} verdict is {e.verdict}",
                        evidence_anchor,
                    )
                )

    open_comments = [
        a for a in view.annotations if a.kind == AnnotationKind.COMMENT.value and a.status == "open"
    ]
    if open_comments:
        findings.append(
            Finding(
                "open_comments",
                ADVISORY,
                f"{len(open_comments)} unresolved comment(s): "
                + ", ".join(a.annotation_id for a in open_comments),
                {"manifest_id": manifest_id},
            )
        )
    for annotation_id in view.stale_approvals:
        findings.append(
            Finding(
                "stale_approval",
                ADVISORY,
                f"approval {annotation_id} is bound to a manifest that is no longer adopted",
                {"manifest_id": manifest_id},
            )
        )
    return findings


def _input_refs(view: EpisodeView, manifest_id: str) -> dict[str, Any]:
    """レビュアー自身の入力 snapshot（同じなら同じ所見になる）。"""
    return {
        "rules_version": REVIEWER_RULES_VERSION,
        "manifest_id": manifest_id,
        "members": {
            m.slot: {"revision_id": m.revision_id, "sha256": m.sha256}
            for m in view.manifest.members
        },
        "stale": {m.slot: m.stale_reasons for m in view.manifest.members if m.stale},
        "comments": sorted(
            f"{a.annotation_id}:{a.status}"
            for a in view.annotations
            if a.kind == AnnotationKind.COMMENT.value
        ),
        "stale_approvals": sorted(view.stale_approvals),
        "evidence": sorted(
            f"{e.research_artifact_id}:{e.sha256}:{e.is_current}" for e in view.evidence
        ),
    }


def _verdict_payload(
    findings: list[Finding], manifest_id: str, view: EpisodeView, snapshot_ref: str
) -> DecisionRecordPayload:
    blocking = [f for f in findings if f.severity == BLOCKING]
    advisory = [f for f in findings if f.severity == ADVISORY]
    verdict = VERDICT_NEEDS_ATTENTION if blocking else VERDICT_PASS
    return DecisionRecordPayload(
        decision_type=REVIEW_VERDICT,
        question=f"Is the adopted manifest {manifest_id} ready to proceed?",
        candidates=(
            DecisionCandidate(
                id=verdict,
                summary=f"{verdict}: {len(blocking)} blocking, {len(advisory)} advisory finding(s)",
                scores={"blocking": float(len(blocking)), "advisory": float(len(advisory))},
                assessment_kind=AssessmentKind.RULE,
            ),
        ),
        selected_candidate=verdict,
        rationale_kind=RationaleKind.RULE,
        rationale=(
            "blocking checks: " + ", ".join(sorted({f.check for f in blocking}))
            if blocking
            else "no blocking findings under " + REVIEWER_RULES_VERSION
        ),
        input_snapshot_ref=snapshot_ref,
        target_revisions=tuple(m.revision_id for m in view.manifest.members),
        uncertainties=(
            "deterministic rules only: no LLM or human judgement of content quality or accuracy",
            "evidence findings are advisory (script verification does not stop an episode)",
        ),
    )


# --------------------------------------------------------------------------- 入口


async def _ensure_manifest(
    session_factory: async_sessionmaker[AsyncSession], episode_id: str
) -> str:
    async with session_factory() as session:
        if await EpisodeRepository(session).get(episode_id) is None:
            raise ValueError(f"episode not found: {episode_id}")
        store = InterventionStore(session)
        manifest = await ensure_manifest(session, store, episode_id, SYSTEM_ACTOR)
        await session.commit()
        return manifest.manifest_id


async def run_reviewer(
    session_factory: async_sessionmaker[AsyncSession],
    artifact_store: ArtifactStore,
    episode_id: str,
) -> ReviewResult:
    """採用 manifest を検査して所見・判定・実行記録を残す。成果物は作らない。"""
    manifest_id = await _ensure_manifest(session_factory, episode_id)
    view = await EpisodeReadModel(session_factory, artifact_store).load(episode_id)
    if view is None or view.manifest.manifest_id != manifest_id:
        raise ReviewerConflictError("the adopted manifest changed while reviewing")

    findings = await _inspect(view, artifact_store, manifest_id)
    input_refs = _input_refs(view, manifest_id)
    digest = hashlib.sha256(canonical_json_bytes(input_refs)).hexdigest()[:24]
    key = f"reviewer:{manifest_id}:{digest}"
    run_id = agent_run_id_for(key)

    async with session_factory() as session:
        store = InterventionStore(session)
        existing = await store.get_agent_run(run_id)
        if existing is not None:
            return await _existing_result(store, episode_id, manifest_id, run_id)

    capture = RunCapture(
        idempotency_key=key,
        role=AgentRole.REVIEWER,
        step=REVIEW_STEP,
        model=None,
        prompt_version=REVIEWER_RULES_VERSION,
        prompt=None,
        prompt_source=PromptSource.NOT_APPLICABLE,
        input_refs=input_refs,
        request=None,
        response={"findings": [f.document() for f in findings]},
        tool_invocations=tuple(
            {"kind": "artifact_read", "id": m.revision_id} for m in view.manifest.members
        ),
        episode_id=episode_id,
    )
    recorder = AgentRunRecorder(artifact_store)
    stored = await recorder.write(capture)

    async with session_factory() as session:
        store = InterventionStore(session)
        control = await store.lock_control(episode_id)
        if control.active_manifest_id != manifest_id:
            raise ReviewerConflictError("the adopted manifest changed while reviewing")
        run = await recorder.insert(session, capture, stored)
        annotation_ids = []
        for finding in findings:
            annotation = await store.insert_annotation(
                episode_id=episode_id,
                kind=AnnotationKind.REVIEW_FINDING,
                anchor=finding.anchor,
                body=f"[{finding.severity}] {finding.check}: {finding.message}",
                actor=REVIEWER_ACTOR,
                payload={
                    "check": finding.check,
                    "severity": finding.severity,
                    "rules_version": REVIEWER_RULES_VERSION,
                },
                manifest_id=manifest_id,
                agent_run_id=run.agent_run_id,
            )
            annotation_ids.append(annotation.annotation_id)
        payload = _verdict_payload(findings, manifest_id, view, stored.snapshot_key)
        decision = await store.insert_decision(
            idempotency_key=f"decision:{REVIEW_VERDICT}:{run.agent_run_id}",
            decision_type=REVIEW_VERDICT,
            actor=REVIEWER_ACTOR,
            payload=payload.model_dump(mode="json"),
            episode_id=episode_id,
            agent_run_id=run.agent_run_id,
        )
        await store.append_event(
            episode_id,
            InterventionEvent.DECISION_RECORDED,
            REVIEWER_ACTOR,
            {
                "decision_id": decision.decision_id,
                "decision_type": REVIEW_VERDICT,
                "agent_run_id": run.agent_run_id,
                "manifest_id": manifest_id,
                "verdict": payload.selected_candidate,
            },
        )
        await session.commit()
    return ReviewResult(
        agent_run_id=run.agent_run_id,
        decision_id=decision.decision_id,
        manifest_id=manifest_id,
        verdict=str(payload.selected_candidate),
        findings=findings,
        annotation_ids=annotation_ids,
        reused=False,
    )


async def _existing_result(
    store: InterventionStore, episode_id: str, manifest_id: str, run_id: str
) -> ReviewResult:
    decision = next(
        d
        for d in await store.list_decisions(episode_id=episode_id)
        if d.agent_run_id == run_id and d.decision_type == REVIEW_VERDICT
    )
    annotations = [
        a
        for a in await store.list_annotations(episode_id, kind=AnnotationKind.REVIEW_FINDING)
        if a.agent_run_id == run_id
    ]
    findings = [
        Finding(
            check=str((a.payload or {}).get("check")),
            severity=str((a.payload or {}).get("severity")),
            message=a.body.split(": ", 1)[-1],
            anchor=dict(a.anchor),
        )
        for a in annotations
    ]
    return ReviewResult(
        agent_run_id=run_id,
        decision_id=decision.decision_id,
        manifest_id=manifest_id,
        verdict=str(decision.payload.get("selected_candidate")),
        findings=findings,
        annotation_ids=[a.annotation_id for a in annotations],
        reused=True,
    )


__all__ = [
    "REVIEW_VERDICT",
    "REVIEWER_RULES_VERSION",
    "Finding",
    "ReviewResult",
    "ReviewerConflictError",
    "run_reviewer",
]
