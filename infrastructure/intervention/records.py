"""DecisionRecord と revision link の導出。

JP-MAS-001 §7 / §12、ADR-0041、``docs/domain/intervention.md`` §6。

**候補・スコアを補完しない。** ここで作る判断記録は、保存済みの実データ（``topic_candidates``・
生成した成果物・予約台帳・ContextSnapshot）だけから組み立てる。

- producer ``choose_topic``: TopicPlan の保存済み候補（最終 round）。モデルの ``reason`` は
  ``rationale_kind=reported``、``audience_fit`` / ``visual_fit`` は自己評価
  （``self_assessed_scores``）、
  ``score`` / ``score_breakdown`` / 却下・重複は決定論の規則（``assessment_kind=rule``）。
- writer / director（``write_script`` / ``plan_storyboard``）: 候補は生成物1件だけ。モデルが理由を
  返さないので ``rationale_kind=none``。根拠の参照があれば ContextSnapshot の本文と照合する。
- revision link: 成果物 JSON の中の参照（``source_script`` 等の ``artifact_id``）が上流。新方式の
  Episode では書き込み（``record_derived_links``）、古い Episode は読み取り時に同じ辺を導出する
  （``derive_links``。何も書かない）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.intervention import (
    ActorType,
    AgentRole,
    AssessmentKind,
    DecisionCandidate,
    DecisionRecordPayload,
    EvidenceCheck,
    InterventionEvent,
    RationaleKind,
    RevisionLinkKind,
)
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.db.repositories import (
    TopicCandidateRecord,
    TopicPlan,
    TopicPlanRepository,
)
from infrastructure.intervention.store import (
    Actor,
    AgentRun,
    DecisionRecord,
    InterventionStore,
)
from infrastructure.storage.artifact_store import ArtifactStore

CHOOSE_TOPIC = "choose_topic"
WRITE_SCRIPT = "write_script"
PLAN_STORYBOARD = "plan_storyboard"

PRODUCER_ACTOR = Actor(ActorType.AGENT, AgentRole.PRODUCER.value)
WRITER_DIRECTOR_ACTOR = Actor(ActorType.AGENT, AgentRole.WRITER_DIRECTOR.value)

#: 自己評価の項目（``TopicCandidate`` の LLM 出力。0..1）
_SELF_ASSESSED_KEYS = ("audience_fit", "visual_fit")

SELF_ASSESSMENT_NOTE = (
    "audience_fit / visual_fit are the model's self-assessment, not viewer measurements; "
    "the rule score uses them as inputs"
)
CONFIGURED_MODEL_NOTE = (
    "model identifier is the configured value; the model the CLI actually used is unverified"
)


# --------------------------------------------------------------------------- 根拠の照合


def check_evidence(evidence_refs: Sequence[str], snapshot_text: str | None) -> EvidenceCheck:
    """根拠の参照が、実際に渡した入力（ContextSnapshot の本文）に現れるか。

    参照が無ければ ``not_checked``。1つでも入力に無ければ ``unverified_ref``（設計 §7）。
    """
    refs = [r for r in evidence_refs if r]
    if not refs:
        return EvidenceCheck.NOT_CHECKED
    if snapshot_text is None:
        return EvidenceCheck.UNVERIFIED_REF
    if all(ref in snapshot_text for ref in refs):
        return EvidenceCheck.VERIFIED_IN_INPUT
    return EvidenceCheck.UNVERIFIED_REF


# --------------------------------------------------------------------------- producer


def _rule_scores(candidate: TopicCandidateRecord) -> dict[str, float] | None:
    scores: dict[str, float] = {}
    if candidate.score is not None:
        scores["score"] = float(candidate.score)
    for key, value in (candidate.score_breakdown or {}).items():
        scores[str(key)] = float(value)
    return scores or None


def _self_assessed(payload: Mapping[str, Any]) -> dict[str, float] | None:
    values = {
        key: float(payload[key])
        for key in _SELF_ASSESSED_KEYS
        if isinstance(payload.get(key), int | float)
    }
    return values or None


def _rejected_reason(
    candidate: TopicCandidateRecord, selected: TopicCandidateRecord | None
) -> str | None:
    if selected is not None and candidate.id == selected.id:
        return None
    if candidate.rejected:
        reason = f"rejected by rule: duplicate level {candidate.duplicate_level.value}"
        reason += f" (duplicate_score {candidate.duplicate_score:g})"
        if candidate.duplicate_of:
            reason += f" of {candidate.duplicate_of!r}"
        return reason
    if selected is not None and candidate.score is not None and selected.score is not None:
        return (
            f"not selected by rule: score {candidate.score:g} "
            f"< selected {selected.score:g} (highest score wins)"
        )
    return "not selected"


def _summary(payload: Mapping[str, Any], fallback: str) -> str:
    topic = payload.get("topic")
    hook = payload.get("hook")
    text = str(topic) if isinstance(topic, str) and topic else fallback
    if isinstance(hook, str) and hook:
        text = f"{text} — {hook}"
    return text[:2000]


def topic_decision_payload(
    plan: TopicPlan,
    candidates: Sequence[TopicCandidateRecord],
    *,
    input_snapshot_ref: str | None = None,
    tool_invocation_refs: Sequence[str] = (),
) -> DecisionRecordPayload | None:
    """保存済みの plan と候補から ``choose_topic`` を作る。候補が無ければ ``None``（作らない）。"""
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda c: c.ordinal)
    selected = next((c for c in ordered if c.id == plan.selected_candidate_id), None)
    decision_candidates = tuple(
        DecisionCandidate(
            id=c.id,
            summary=_summary(c.payload, c.subject),
            benefits=(str(c.payload["reason"])[:2000],)
            if isinstance(c.payload.get("reason"), str)
            else (),
            scores=_rule_scores(c),
            assessment_kind=AssessmentKind.RULE if _rule_scores(c) else None,
            self_assessed_scores=_self_assessed(c.payload),
            rejected_reason=_rejected_reason(c, selected),
        )
        for c in ordered
    )
    reported = None if selected is None else selected.payload.get("reason")
    uncertainties = [SELF_ASSESSMENT_NOTE]
    final_round = max(c.round for c in ordered)
    if final_round > 1:
        uncertainties.append(
            f"candidates of rounds 1..{final_round - 1} were not persisted "
            "(only the round that produced the plan is stored)"
        )
    if selected is None:
        uncertainties.append("the selected candidate row is missing")
    return DecisionRecordPayload(
        decision_type=CHOOSE_TOPIC,
        question=(
            f"Which topic to plan for {plan.plan_date.isoformat()} "
            f"({plan.strategy_profile_id}@{plan.strategy_version} / "
            f"{plan.content_profile_id}@{plan.content_profile_version})?"
        ),
        candidates=decision_candidates,
        selected_candidate=None if selected is None else selected.id,
        rationale_kind=RationaleKind.REPORTED
        if isinstance(reported, str) and reported
        else RationaleKind.NONE,
        rationale=reported[:4000] if isinstance(reported, str) and reported else None,
        input_snapshot_ref=input_snapshot_ref,
        tool_invocation_refs=tuple(tool_invocation_refs),
        constraints=(
            f"strategy_profile={plan.strategy_profile_id}@{plan.strategy_version}",
            f"content_profile={plan.content_profile_id}@{plan.content_profile_version}",
            f"analytics_mode={plan.analytics_mode.value}",
            f"planner_version={plan.planner_version}",
            f"prompt_version={plan.prompt_version}",
        ),
        uncertainties=tuple(uncertainties),
    )


async def derive_topic_decision(
    session: AsyncSession,
    topic_plan_id: str,
    *,
    input_snapshot_ref: str | None = None,
    tool_invocation_refs: Sequence[str] = (),
) -> DecisionRecordPayload | None:
    """既存の plan から ``choose_topic`` を導く（書かない）。plan・候補が無ければ ``None``。"""
    plans = TopicPlanRepository(session)
    try:
        plan = await plans.get(topic_plan_id)
    except ValueError:
        return None
    if plan is None:
        return None
    return topic_decision_payload(
        plan,
        await plans.list_candidates(topic_plan_id),
        input_snapshot_ref=input_snapshot_ref,
        tool_invocation_refs=tool_invocation_refs,
    )


async def record_topic_decision(
    session: AsyncSession,
    topic_plan_id: str,
    *,
    agent_run_id: str | None = None,
    input_snapshot_ref: str | None = None,
    tool_invocation_refs: Sequence[str] = (),
) -> DecisionRecord | None:
    """``choose_topic`` を ``decision_records`` に書く（冪等）。commit は呼び出し側。"""
    payload = await derive_topic_decision(
        session,
        topic_plan_id,
        input_snapshot_ref=input_snapshot_ref,
        tool_invocation_refs=tool_invocation_refs,
    )
    if payload is None:
        return None
    return await InterventionStore(session).insert_decision(
        idempotency_key=f"decision:{CHOOSE_TOPIC}:{topic_plan_id}",
        decision_type=CHOOSE_TOPIC,
        actor=PRODUCER_ACTOR,
        payload=payload.model_dump(mode="json"),
        topic_plan_id=topic_plan_id,
        agent_run_id=agent_run_id,
    )


# --------------------------------------------------------------------------- writer / director


def generation_decision_payload(
    *,
    decision_type: str,
    question: str,
    candidate_summary: str,
    result_revision: str,
    input_snapshot_ref: str | None,
    tool_invocation_refs: Sequence[str] = (),
    target_revisions: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    snapshot_text: str | None = None,
    reported_rationale: str | None = None,
    uncertainties: Sequence[str] = (CONFIGURED_MODEL_NOTE,),
) -> DecisionRecordPayload:
    """生成1回の判断記録。候補は生成物1件（設計 §7「候補が一つなら一つと記録する」）。"""
    return DecisionRecordPayload(
        decision_type=decision_type,
        question=question[:2000],
        candidates=(DecisionCandidate(id="generated", summary=candidate_summary[:2000]),),
        selected_candidate="generated",
        rationale_kind=RationaleKind.REPORTED if reported_rationale else RationaleKind.NONE,
        rationale=reported_rationale[:4000] if reported_rationale else None,
        evidence_refs=tuple(evidence_refs),
        evidence_check=check_evidence(evidence_refs, snapshot_text),
        input_snapshot_ref=input_snapshot_ref,
        tool_invocation_refs=tuple(tool_invocation_refs),
        uncertainties=tuple(uncertainties),
        target_revisions=tuple(target_revisions),
        result_revision=result_revision,
    )


async def record_generation_decision(
    session: AsyncSession,
    *,
    episode_id: str,
    agent_run: AgentRun,
    payload: DecisionRecordPayload,
) -> DecisionRecord:
    """writer / director の判断記録と ``decision.recorded`` を足す（冪等）。commit は呼び出し側。"""
    store = InterventionStore(session)
    key = f"decision:{payload.decision_type}:{agent_run.agent_run_id}"
    for decision in await store.list_decisions(episode_id=episode_id):
        if decision.agent_run_id == agent_run.agent_run_id and (
            decision.decision_type == payload.decision_type
        ):
            return decision
    decision = await store.insert_decision(
        idempotency_key=key,
        decision_type=payload.decision_type,
        actor=WRITER_DIRECTOR_ACTOR,
        payload=payload.model_dump(mode="json"),
        episode_id=episode_id,
        agent_run_id=agent_run.agent_run_id,
        result_revision_id=payload.result_revision,
    )
    await store.append_event(
        episode_id,
        InterventionEvent.DECISION_RECORDED,
        WRITER_DIRECTOR_ACTOR,
        {
            "decision_id": decision.decision_id,
            "decision_type": payload.decision_type,
            "agent_run_id": agent_run.agent_run_id,
            "result_revision_id": payload.result_revision,
        },
    )
    return decision


# --------------------------------------------------------------------------- revision link


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def json_upstream_refs(payload: Mapping[str, Any]) -> list[str]:
    """成果物 JSON の中の上流参照（入れ子の ``{"artifact_id": <uuid>}``）。出現順・重複なし。"""
    found: dict[str, None] = {}

    def walk(value: Any, *, root: bool) -> None:
        if isinstance(value, Mapping):
            if not root and _is_uuid(value.get("artifact_id")):
                found.setdefault(str(value["artifact_id"]), None)
            for child in value.values():
                walk(child, root=False)
        elif isinstance(value, list | tuple):
            for child in value:
                walk(child, root=False)

    walk(payload, root=True)
    return list(found)


async def _existing_ids(session: AsyncSession, episode_id: str, ids: Iterable[str]) -> set[str]:
    wanted = [uuid.UUID(i) for i in ids]
    if not wanted:
        return set()
    rows = await session.scalars(
        select(ArtifactMetadataRow.id).where(
            ArtifactMetadataRow.episode_id == uuid.UUID(str(episode_id)),
            ArtifactMetadataRow.id.in_(wanted),
        )
    )
    return {str(r) for r in rows}


async def record_derived_links(
    session: AsyncSession,
    episode_id: str,
    downstream_revision_id: str,
    payload: Mapping[str, Any],
) -> list[str]:
    """新しい成果物の ``derived_from`` を JSON の参照から書く（冪等）。commit は呼び出し側。

    同じ Episode の ``artifact_metadata`` に無い参照は辺にしない（存在しない revision を指さない）。
    """
    refs = json_upstream_refs(payload)
    existing = await _existing_ids(session, episode_id, refs)
    store = InterventionStore(session)
    linked: list[str] = []
    for upstream in refs:
        if upstream not in existing or upstream == downstream_revision_id:
            continue
        await store.add_link(
            episode_id, upstream, downstream_revision_id, RevisionLinkKind.DERIVED_FROM
        )
        linked.append(upstream)
    return linked


@dataclass(frozen=True, slots=True)
class DerivedLink:
    upstream_revision_id: str
    downstream_revision_id: str
    kind: RevisionLinkKind = RevisionLinkKind.DERIVED_FROM


@dataclass(frozen=True, slots=True)
class DerivedLinks:
    edges: list[DerivedLink] = field(default_factory=list)
    #: 本体を読めなかった revision（辺を導けない。黙って飛ばさず列挙する）
    unreadable: list[str] = field(default_factory=list)


async def derive_links(
    session: AsyncSession, artifact_store: ArtifactStore, episode_id: str
) -> DerivedLinks:
    """古い Episode も含め、成果物 JSON の参照から ``derived_from`` の辺を導く。**書かない。**"""
    rows = list(
        await session.scalars(
            select(ArtifactMetadataRow)
            .where(ArtifactMetadataRow.episode_id == uuid.UUID(str(episode_id)))
            .order_by(ArtifactMetadataRow.created_at, ArtifactMetadataRow.id)
        )
    )
    known = {str(r.id) for r in rows}
    result = DerivedLinks()
    for row in rows:
        try:
            payload = await artifact_store.get_json(row.object_key)
        except Exception:  # noqa: BLE001 - 読めないことを結果として返す
            result.unreadable.append(str(row.id))
            continue
        for upstream in json_upstream_refs(payload):
            if upstream in known and upstream != str(row.id):
                result.edges.append(DerivedLink(upstream, str(row.id)))
    return result


__all__ = [
    "CHOOSE_TOPIC",
    "CONFIGURED_MODEL_NOTE",
    "PLAN_STORYBOARD",
    "WRITE_SCRIPT",
    "DerivedLink",
    "DerivedLinks",
    "check_evidence",
    "derive_links",
    "derive_topic_decision",
    "generation_decision_payload",
    "json_upstream_refs",
    "record_derived_links",
    "record_generation_decision",
    "record_topic_decision",
    "topic_decision_payload",
]
