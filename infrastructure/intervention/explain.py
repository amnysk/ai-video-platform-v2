"""判断の説明と Episode 横断の判断検索（ADR-0042 §5）。**読み取り専用・LLM を呼ばない**。

説明は保存済みの行（``decision_records`` / ``agent_runs`` / ``annotations`` /
``intervention_commands`` / ``revision_links``）だけから決定的に組み立てる。

- 記録の無い項目は ``null`` と ``not_recorded`` の一覧で返し、補完しない（設計 §7）。
- ``rationale_kind`` は保存された値をそのまま返す。``reported`` はモデルの自己申告で、
  検証済みの事実として扱わない（``provenance`` / ``verified``）。
- token 使用量・費用は ``agent_runs`` が NULL なら ``null``（0 で埋めない）。
- 書き込み・commit をしない（INV-16: 重い処理をしない）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.intervention import AgentRole
from infrastructure.db.models import AgentRunRow, DecisionRecordRow
from infrastructure.intervention.store import (
    AgentRun,
    DecisionRecord,
    InterventionStore,
    _to_decision,
)

NOT_RECORDED = "not_recorded"

#: ``rationale_kind`` → 理由の出どころの表示。``reported`` は自己申告であって検証ではない
_PROVENANCE: dict[str, str] = {
    "reported": "self_reported_by_model",
    "post_hoc_summary": "post_hoc_generated_summary",
    "human": "human_stated",
    "rule": "deterministic_rule",
}

DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ActorOut(_View):
    type: str
    id: str


class CandidateOut(_View):
    id: str | None
    summary: str | None
    selected: bool
    benefits: list[Any]
    concerns: list[Any]
    scores: dict[str, Any] | None
    assessment_kind: str | None
    self_assessed_scores: dict[str, Any] | None
    rejected_reason: str | None


class RationaleOut(_View):
    #: 保存された ``rationale_kind`` をそのまま
    kind: str
    text: str | None
    #: 出どころ。``reported`` = ``self_reported_by_model``。記録が無ければ ``not_recorded``
    provenance: str
    #: この説明の作成者が検証した事実ではない（常に False）
    verified: bool = False


class EvidenceOut(_View):
    refs: list[str]
    #: 保存された ``evidence_check`` をそのまま
    evidence_check: str


class AgentRunOut(_View):
    agent_run_id: str
    role: str
    step: str
    model: str | None
    prompt_version: str | None
    outcome: str
    snapshot_sha256: str
    request_sha256: str | None
    response_sha256: str | None
    tool_invocations: list[dict[str, Any]]
    #: 取得できなければ null（0 で埋めない）
    token_usage: dict[str, Any] | None
    estimated_cost_usd: float | None
    started_at: datetime | None
    finished_at: datetime | None


class DownstreamOut(_View):
    upstream_revision_id: str
    downstream_revision_id: str
    kind: str
    causation_command_id: str | None


class AnnotationOut(_View):
    annotation_id: str
    kind: str
    anchor: dict[str, Any]
    body: str
    actor: ActorOut
    command_id: str | None
    created_at: datetime


class CommandOut(_View):
    command_id: str
    type: str
    status: str
    actor: ActorOut
    reject_reason: str | None
    expected_target_revision: str | None
    received_at: datetime


class InterventionsOut(_View):
    annotations: list[AnnotationOut]
    commands: list[CommandOut]


class DecisionExplanation(_View):
    decision_id: str
    episode_id: str | None
    decision_type: str
    recorded_at: datetime
    actor: ActorOut
    question: str | None
    candidates: list[CandidateOut]
    selected_candidate: str | None
    rationale: RationaleOut
    evidence: EvidenceOut
    constraints: list[str]
    uncertainties: list[str]
    input_snapshot_ref: str | None
    target_revisions: list[str]
    result_revision: str | None
    override_command_id: str | None
    agent_run: AgentRunOut | None
    interventions: InterventionsOut
    downstream_revisions: list[DownstreamOut]
    #: 記録が無い項目の名前。ここにある項目を呼び出し側が推測で埋めない
    not_recorded: list[str]


class DecisionSummary(_View):
    decision_id: str
    episode_id: str | None
    decision_type: str
    role: str | None
    actor: ActorOut
    question: str | None
    selected_candidate: str | None
    rationale_kind: str | None
    evidence_check: str | None
    result_revision_id: str | None
    created_at: datetime


class DecisionPage(_View):
    decisions: list[DecisionSummary]
    #: 次ページの ``before``（末尾の decision_id）。続きが無ければ null
    next_before: str | None


class InvalidCursorError(ValueError):
    pass


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _candidate(raw: Any, selected: str | None) -> CandidateOut:
    c: dict[str, Any] = raw if isinstance(raw, dict) else {}
    cid = _opt_str(c.get("id"))
    return CandidateOut(
        id=cid,
        summary=_opt_str(c.get("summary")),
        selected=selected is not None and cid == selected,
        benefits=list(c.get("benefits") or []),
        concerns=list(c.get("concerns") or []),
        scores=c.get("scores"),
        assessment_kind=_opt_str(c.get("assessment_kind")),
        self_assessed_scores=c.get("self_assessed_scores"),
        rejected_reason=_opt_str(c.get("rejected_reason")),
    )


def _agent_run_out(run: AgentRun) -> AgentRunOut:
    cost = run.estimated_cost_usd
    return AgentRunOut(
        agent_run_id=run.agent_run_id,
        role=run.role.value,
        step=run.step,
        model=run.model,
        prompt_version=run.prompt_version,
        outcome=run.outcome.value,
        snapshot_sha256=run.snapshot_sha256,
        request_sha256=run.request_sha256,
        response_sha256=run.response_sha256,
        tool_invocations=run.tool_invocations,
        token_usage=run.token_usage,
        estimated_cost_usd=None if cost is None else float(cost),
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _revisions(decision: DecisionRecord) -> tuple[list[str], str | None]:
    payload = decision.payload
    result = decision.result_revision_id or _opt_str(payload.get("result_revision"))
    targets = _strings(payload.get("target_revisions"))
    return targets, result


async def explain_decision(
    session: AsyncSession, episode_id: str, decision_id: str
) -> DecisionExplanation | None:
    """``None`` = その Episode にその判断は無い。"""
    store = InterventionStore(session)
    row = await session.get(DecisionRecordRow, uuid.UUID(decision_id))
    if row is None or row.episode_id is None or str(row.episode_id) != episode_id:
        return None
    decision = _to_decision(row)
    payload = decision.payload
    selected = _opt_str(payload.get("selected_candidate"))
    targets, result = _revisions(decision)
    revisions: list[str] = list(dict.fromkeys([*targets, *([result] if result else [])]))
    missing: list[str] = []

    kind = _opt_str(payload.get("rationale_kind")) or NOT_RECORDED
    text = _opt_str(payload.get("rationale"))
    provenance = _PROVENANCE.get(kind, NOT_RECORDED) if text is not None else NOT_RECORDED
    if text is None or kind in {"none", NOT_RECORDED}:
        missing.append("rationale")

    refs = _strings(payload.get("evidence_refs"))
    if not refs:
        missing.append("evidence_refs")
    snapshot_ref = _opt_str(payload.get("input_snapshot_ref"))
    if snapshot_ref is None:
        missing.append("input_snapshot_ref")
    if result is None:
        missing.append("result_revision")

    run: AgentRun | None = None
    if decision.agent_run_id is not None:
        run = await store.get_agent_run(decision.agent_run_id)
    if run is None:
        missing.append("agent_run")
    else:
        if run.model is None:
            missing.append("agent_run.model")
        if run.prompt_version is None:
            missing.append("agent_run.prompt_version")
        if run.token_usage is None:
            missing.append("agent_run.token_usage")
        if run.estimated_cost_usd is None:
            missing.append("agent_run.estimated_cost_usd")

    override = _opt_str(payload.get("override_command_id"))
    annotations = [
        AnnotationOut(
            annotation_id=a.annotation_id,
            kind=a.kind.value,
            anchor=a.anchor,
            body=a.body,
            actor=ActorOut(type=a.actor.type.value, id=a.actor.id),
            command_id=a.command_id,
            created_at=a.created_at,
        )
        for a in await store.list_annotations(episode_id)
        if a.anchor.get("revision_id") in revisions
    ]
    commands = [
        CommandOut(
            command_id=c.command_id,
            type=c.type.value,
            status=c.status.value,
            actor=ActorOut(type=c.actor.type.value, id=c.actor.id),
            reject_reason=None if c.reject_reason is None else c.reject_reason.value,
            expected_target_revision=c.expected_target_revision,
            received_at=c.received_at,
        )
        for c in await store.list_commands(episode_id)
        if c.expected_target_revision in revisions
        or _base_revision(c.payload) in revisions
        or (override is not None and c.command_id == override)
    ]
    downstream: list[DownstreamOut] = []
    for revision in revisions:
        for link in await store.links(upstream_revision_id=revision):
            downstream.append(
                DownstreamOut(
                    upstream_revision_id=link.upstream_revision_id,
                    downstream_revision_id=link.downstream_revision_id,
                    kind=link.kind.value,
                    causation_command_id=link.causation_command_id,
                )
            )
    return DecisionExplanation(
        decision_id=decision.decision_id,
        episode_id=decision.episode_id,
        decision_type=decision.decision_type,
        recorded_at=decision.created_at,
        actor=ActorOut(type=decision.actor.type.value, id=decision.actor.id),
        question=_opt_str(payload.get("question")),
        candidates=[_candidate(c, selected) for c in payload.get("candidates") or []],
        selected_candidate=selected,
        rationale=RationaleOut(kind=kind, text=text, provenance=provenance),
        evidence=EvidenceOut(
            refs=refs, evidence_check=_opt_str(payload.get("evidence_check")) or NOT_RECORDED
        ),
        constraints=_strings(payload.get("constraints")),
        uncertainties=_strings(payload.get("uncertainties")),
        input_snapshot_ref=snapshot_ref,
        target_revisions=targets,
        result_revision=result,
        override_command_id=override,
        agent_run=None if run is None else _agent_run_out(run),
        interventions=InterventionsOut(annotations=annotations, commands=commands),
        downstream_revisions=downstream,
        not_recorded=missing,
    )


def _base_revision(payload: dict[str, Any]) -> str | None:
    change = payload.get("change")
    return _opt_str(change.get("base_revision_id")) if isinstance(change, dict) else None


async def search_decisions(
    session: AsyncSession,
    *,
    decision_type: str | None = None,
    role: AgentRole | None = None,
    episode_id: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    before: str | None = None,
) -> DecisionPage:
    """新しい順（``created_at``, ``id`` の降順）の keyset pagination。正本の DB だけを読む。"""
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    stmt = select(DecisionRecordRow, AgentRunRow.role).outerjoin(
        AgentRunRow, DecisionRecordRow.agent_run_id == AgentRunRow.id
    )
    if decision_type is not None:
        stmt = stmt.where(DecisionRecordRow.decision_type == decision_type)
    if role is not None:
        stmt = stmt.where(AgentRunRow.role == role.value)
    if episode_id is not None:
        stmt = stmt.where(DecisionRecordRow.episode_id == uuid.UUID(episode_id))
    if before is not None:
        try:
            cursor = await session.get(DecisionRecordRow, uuid.UUID(before))
        except ValueError:
            raise InvalidCursorError(before) from None
        if cursor is None:
            raise InvalidCursorError(before)
        stmt = stmt.where(
            or_(
                DecisionRecordRow.created_at < cursor.created_at,
                and_(
                    DecisionRecordRow.created_at == cursor.created_at,
                    DecisionRecordRow.id < cursor.id,
                ),
            )
        )
    stmt = stmt.order_by(DecisionRecordRow.created_at.desc(), DecisionRecordRow.id.desc()).limit(
        limit + 1
    )
    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    summaries: list[DecisionSummary] = []
    for decision_row, run_role in page:
        d = _to_decision(decision_row)
        summaries.append(
            DecisionSummary(
                decision_id=d.decision_id,
                episode_id=d.episode_id,
                decision_type=d.decision_type,
                role=run_role,
                actor=ActorOut(type=d.actor.type.value, id=d.actor.id),
                question=_opt_str(d.payload.get("question")),
                selected_candidate=_opt_str(d.payload.get("selected_candidate")),
                rationale_kind=_opt_str(d.payload.get("rationale_kind")),
                evidence_check=_opt_str(d.payload.get("evidence_check")),
                result_revision_id=d.result_revision_id,
                created_at=d.created_at,
            )
        )
    next_before = summaries[-1].decision_id if len(rows) > limit and summaries else None
    return DecisionPage(decisions=summaries, next_before=next_before)
