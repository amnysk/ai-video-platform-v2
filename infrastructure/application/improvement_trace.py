"""改善トレースの組み立て（ADR-0047 §6・§7）。**何も書かず、保存済みの記録だけから決定的に組む。**

鎖: tool 呼び出し（``agent_tool_calls``）→ 判断記録（payload / 成果物の ``tool_call:<id>`` 引用）→
戦略案・演出指針・品質レビュー・Lesson（``artifact_metadata`` + ``revision_links(derived_from)``）→
投稿受領（video id）→ Lesson（測定の結果と次の提案）。値が無い鎖は None/空 + ``missing`` の理由で
明示する。測っていない結果を作らない（``TraceResult.status``）。新しい関係表は無い。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agentic import (
    CreativeBrief,
    FactCheckReport,
    Lesson,
    QualityReview,
    StrategyBrief,
)
from contracts.application_operations import (
    ImprovementTrace,
    ToolCallSummary,
    TraceArtifact,
    TraceDecision,
    TraceIntendedImprovement,
    TraceNextProposal,
    TracePublication,
    TraceQualityReview,
    TraceReflection,
    TraceResult,
    TraceToolCall,
    TraceTopicSelection,
)
from contracts.artifact_registry import ARTIFACT_REGISTRY, parse_artifact
from contracts.artifacts import ARTIFACT_MODELS
from contracts.intervention import RevisionLinkKind, TargetRefKind
from contracts.states import ArtifactType
from contracts.upload import UploadReceiptArtifact
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.intervention.read_model import DecisionView, EpisodeView
from infrastructure.intervention.records import CHOOSE_TOPIC
from infrastructure.storage.artifact_store import ArtifactStore

_TOOL_CALL_PREFIX = f"{TargetRefKind.TOOL_CALL.value}:"


def _artifact(row: ArtifactMetadataRow, ups: dict[str, list[str]]) -> TraceArtifact:
    return TraceArtifact(
        artifact_id=str(row.id),
        artifact_type=row.artifact_type,
        version=row.version,
        schema_version=row.schema_version,
        sha256=row.sha256,
        created_at=row.created_at,
        derived_from=tuple(ups.get(str(row.id), ())),
        is_current=row.superseded_at is None,
    )


def _tool_call_ids(refs: Any) -> list[str]:
    return [
        r.removeprefix(_TOOL_CALL_PREFIX)
        for r in (refs or ())
        if isinstance(r, str) and r.startswith(_TOOL_CALL_PREFIX)
    ]


def _decision(d: DecisionView, view: EpisodeView) -> TraceDecision:
    p = d.payload
    candidates = tuple(
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "rejected_reason": c.get("rejected_reason"),
        }
        for c in p.get("candidates") or ()
        if isinstance(c, dict)
    )
    cited = dict.fromkeys(
        _tool_call_ids(p.get("tool_invocation_refs")) + _tool_call_ids(p.get("evidence_refs"))
    )
    target = f"decision:{d.decision_id}"
    return TraceDecision(
        decision_id=d.decision_id,
        decision_type=d.decision_type,
        actor_type=d.actor_type,
        actor_id=d.actor_id,
        agent_run_id=d.agent_run_id,
        question=str(p.get("question", "")),
        selected_candidate=p.get("selected_candidate"),
        candidates=candidates,
        rationale_kind=str(p.get("rationale_kind", "")),
        rationale=p.get("rationale"),
        evidence_check=str(p.get("evidence_check", "")),
        uncertainties=tuple(p.get("uncertainties") or ()),
        cited_tool_call_ids=tuple(cited),
        result_revision_id=d.result_revision_id,
        created_at=d.created_at,
        annotations=tuple(
            a.model_dump(mode="json")
            for a in view.annotations
            if d.decision_id is not None and a.anchor.get("target_ref") == target
        ),
    )


class _Loaded:
    """成果物の読み込み結果。読めなければ ``problem`` に理由（鎖の欠落理由に使う）。"""

    def __init__(self) -> None:
        self.parsed: dict[str, BaseModel] = {}
        self.rows: dict[str, ArtifactMetadataRow] = {}
        self.problems: dict[str, str] = {}


def _parse(artifact_type: ArtifactType, document: dict[str, Any]) -> BaseModel:
    """版つきの registry にある型はそれで、無い型（投稿受領・事実確認）は型の唯一の登録で読む。"""
    if (artifact_type.value, str(document.get("schema_version"))) in ARTIFACT_REGISTRY:
        return parse_artifact(artifact_type, document)
    return ARTIFACT_MODELS[artifact_type].model_validate(document)


async def _load_current(
    rows: list[ArtifactMetadataRow], store: ArtifactStore, types: tuple[ArtifactType, ...]
) -> _Loaded:
    out = _Loaded()
    for artifact_type in types:
        current = [
            r
            for r in rows
            if r.artifact_type == artifact_type.value
            and r.superseded_at is None
            and r.scene_id is None
        ]
        if not current:
            continue
        row = max(current, key=lambda r: r.version)
        out.rows[artifact_type.value] = row
        try:
            out.parsed[artifact_type.value] = _parse(
                artifact_type, await store.get_json(row.object_key)
            )
        except Exception as exc:  # noqa: BLE001 - 読めない成果物は鎖の欠落として理由を残す
            out.problems[artifact_type.value] = f"unreadable: {type(exc).__name__}"
    return out


def _absent(loaded: _Loaded, artifact_type: ArtifactType, what: str) -> str:
    key = artifact_type.value
    if key in loaded.problems:
        return f"{what} exists but could not be read ({loaded.problems[key]})"
    return f"no {what} has been recorded for this episode"


def _downstream(starts: list[str], ups: dict[str, list[str]]) -> list[str]:
    """``derived_from`` で starts から下流へ辿れる revision id（決定的な順）。"""
    children: dict[str, list[str]] = defaultdict(list)
    for down, upstreams in ups.items():
        for up in upstreams:
            children[up].append(down)
    seen: dict[str, None] = {}
    queue = list(starts)
    while queue:
        for child in sorted(children.get(queue.pop(0), ())):
            if child not in seen and child not in starts:
                seen[child] = None
                queue.append(child)
    return list(seen)


async def build_improvement_trace(
    *,
    episode_id: uuid.UUID,
    topic: str | None,
    view: EpisodeView,
    tool_calls: list[ToolCallSummary],
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
) -> ImprovementTrace:
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ArtifactMetadataRow)
                    .where(ArtifactMetadataRow.episode_id == episode_id)
                    .order_by(ArtifactMetadataRow.created_at, ArtifactMetadataRow.version)
                )
            ).all()
        )
    ups: dict[str, list[str]] = defaultdict(list)
    for link in [*view.recorded_links, *view.derived_links]:
        edge = ups[link.downstream_revision_id]
        if (
            link.kind == RevisionLinkKind.DERIVED_FROM.value
            and link.upstream_revision_id not in edge
        ):
            edge.append(link.upstream_revision_id)
    by_id = {str(r.id): r for r in rows}

    loaded = await _load_current(
        rows,
        store,
        (
            ArtifactType.STRATEGY_BRIEF,
            ArtifactType.CREATIVE_BRIEF,
            ArtifactType.QUALITY_REVIEW,
            ArtifactType.UPLOAD_RECEIPT,
            ArtifactType.LESSON,
            ArtifactType.FACT_CHECK_REPORT,
        ),
    )
    strategy = loaded.parsed.get(ArtifactType.STRATEGY_BRIEF.value)
    creative = loaded.parsed.get(ArtifactType.CREATIVE_BRIEF.value)
    review = loaded.parsed.get(ArtifactType.QUALITY_REVIEW.value)
    receipt = loaded.parsed.get(ArtifactType.UPLOAD_RECEIPT.value)
    lesson = loaded.parsed.get(ArtifactType.LESSON.value)
    fact_check = loaded.parsed.get(ArtifactType.FACT_CHECK_REPORT.value)

    def art(artifact_type: ArtifactType) -> TraceArtifact | None:
        row = loaded.rows.get(artifact_type.value)
        return None if row is None else _artifact(row, ups)

    # ---- 判断記録と引用
    decisions = [_decision(d, view) for d in view.decisions]
    strategy_row = loaded.rows.get(ArtifactType.STRATEGY_BRIEF.value)
    cited_by: dict[str, list[str]] = defaultdict(list)
    for d in decisions:
        for cid in d.cited_tool_call_ids:
            cited_by[cid].append(f"decision:{d.decision_id}")
    if isinstance(strategy, StrategyBrief) and strategy_row is not None:
        for cid in _tool_call_ids(strategy.citations):
            cited_by[cid].append(f"{ArtifactType.STRATEGY_BRIEF.value}:{strategy_row.id}")
    fact_row = loaded.rows.get(ArtifactType.FACT_CHECK_REPORT.value)
    if isinstance(fact_check, FactCheckReport) and fact_row is not None:
        for claim in fact_check.claims:
            for cid in _tool_call_ids(claim.evidence_refs):
                cited_by[cid].append(f"{ArtifactType.FACT_CHECK_REPORT.value}:{fact_row.id}")

    known = {c.tool_call_id for c in tool_calls}
    traced_calls = tuple(
        TraceToolCall(
            **c.model_dump(), cited_by=tuple(dict.fromkeys(cited_by.get(c.tool_call_id, ())))
        )
        for c in tool_calls
    )
    unresolved = tuple(f"{_TOOL_CALL_PREFIX}{cid}" for cid in cited_by if cid not in known)

    missing: dict[str, str] = {}
    if not traced_calls:
        missing["tool_calls"] = "no agent tool call has been recorded for this episode"

    # ---- 題材選定
    strategy_id = None if strategy_row is None else str(strategy_row.id)
    topic_decisions = tuple(
        d
        for d in decisions
        if d.result_revision_id == strategy_id or d.decision_type == CHOOSE_TOPIC
    )
    topic_selection: TraceTopicSelection | None = None
    if topic_decisions or isinstance(strategy, StrategyBrief):
        recommended = None
        if isinstance(strategy, StrategyBrief):
            chosen = next(
                c for c in strategy.candidates if c.id == strategy.recommended_candidate_id
            )
            recommended = chosen.model_dump(mode="json")
        topic_selection = TraceTopicSelection(
            decisions=topic_decisions,
            strategy_brief=art(ArtifactType.STRATEGY_BRIEF),
            audience_hypothesis=getattr(strategy, "audience_hypothesis", None),
            audience_basis=getattr(strategy, "audience_basis", None),
            recommended_candidate=recommended,
            citations=tuple(getattr(strategy, "citations", ())),
        )
    else:
        missing["topic_selection"] = (
            _absent(loaded, ArtifactType.STRATEGY_BRIEF, "strategy brief")
            + " and no topic decision is recorded"
        )

    # ---- 何を良くするつもりだったか
    intended: TraceIntendedImprovement | None = None
    if isinstance(strategy, StrategyBrief) or isinstance(creative, CreativeBrief):
        intended = TraceIntendedImprovement(
            success_criteria=tuple(getattr(strategy, "success_criteria", ())),
            retention_hypotheses=tuple(getattr(creative, "retention_hypotheses", ())),
            lessons_used=tuple(
                u.model_dump(mode="json") for u in getattr(strategy, "lessons_used", ())
            ),
        )
    else:
        missing["intended_improvement"] = "no strategy brief or creative brief is readable"

    # ---- 動画への反映
    reflected: TraceReflection | None = None
    if isinstance(creative, CreativeBrief):
        starts = [
            str(loaded.rows[t.value].id)
            for t in (ArtifactType.STRATEGY_BRIEF, ArtifactType.CREATIVE_BRIEF)
            if t.value in loaded.rows
        ]
        derived_rows = [by_id[i] for i in _downstream(starts, ups) if i in by_id]

        def revisions(artifact_type: ArtifactType) -> tuple[TraceArtifact, ...]:
            return tuple(
                _artifact(r, ups)
                for r in rows
                if r.artifact_type == artifact_type.value and r.scene_id is None
            )

        reflected = TraceReflection(
            creative_brief=art(ArtifactType.CREATIVE_BRIEF),
            adopted_candidate_id=creative.adopted_candidate_id,
            topic=creative.topic,
            hook=creative.hook,
            visual_style_notes=tuple(creative.visual_style_notes),
            user_constraints_applied=tuple(creative.user_constraints_applied),
            derived_artifacts=tuple(_artifact(r, ups) for r in derived_rows),
            storyboard_revisions=revisions(ArtifactType.STORYBOARD),
            script_revisions=revisions(ArtifactType.SCRIPT),
        )
    else:
        missing["reflected_in_video"] = _absent(
            loaded, ArtifactType.CREATIVE_BRIEF, "creative brief"
        )

    # ---- 品質レビュー
    quality: TraceQualityReview | None = None
    quality_art = art(ArtifactType.QUALITY_REVIEW)
    if isinstance(review, QualityReview) and quality_art is not None:
        counts: dict[str, int] = defaultdict(int)
        for f in review.findings:
            counts[f.severity] += 1
        quality = TraceQualityReview(
            artifact=quality_art,
            overall=review.overall,
            findings_by_severity=dict(counts),
            inputs_missing=tuple(review.inputs_missing),
            assessment_kind=review.assessment_kind,
        )
    else:
        missing["quality_review"] = _absent(loaded, ArtifactType.QUALITY_REVIEW, "quality review")

    # ---- 投稿
    publication: TracePublication | None = None
    receipt_art = art(ArtifactType.UPLOAD_RECEIPT)
    if isinstance(receipt, UploadReceiptArtifact) and receipt_art is not None:
        publication = TracePublication(
            receipt=receipt_art, video_id=receipt.video_id, privacy_status=receipt.privacy_status
        )
    else:
        missing["publication"] = _absent(loaded, ArtifactType.UPLOAD_RECEIPT, "upload receipt")

    # ---- 結果（常にある。測っていないことを明示する）
    lesson_art = art(ArtifactType.LESSON)
    if isinstance(lesson, Lesson) and lesson_art is not None:
        measured = [
            w.window
            for w in lesson.measurement_windows
            if any(
                m is not None
                for m in (
                    w.views,
                    w.subscribers_gained,
                    w.average_view_duration_seconds,
                    w.average_view_percentage,
                )
            )
        ]
        result = TraceResult(
            status="measured" if measured else "awaiting_measurement",
            reason=(
                f"measured windows: {', '.join(measured)}"
                if measured
                else "a lesson is recorded but no window has a measured metric"
            ),
            lesson=lesson_art,
            lesson_status=lesson.status.value,
            measurement_windows=tuple(
                w.model_dump(mode="json") for w in lesson.measurement_windows
            ),
            observations=tuple(lesson.observations),
        )
    elif publication is None:
        result = TraceResult(
            status="not_published", reason="no upload receipt: the episode is not published"
        )
    else:
        result = TraceResult(
            status="awaiting_measurement",
            reason=_absent(loaded, ArtifactType.LESSON, "lesson")
            + "; the published video has not been measured yet",
        )

    # ---- 次の提案
    next_proposal: TraceNextProposal | None = None
    if isinstance(lesson, Lesson) and (lesson.hypotheses or lesson.proposed_experiments):
        next_proposal = TraceNextProposal(
            lesson_status=lesson.status.value,
            hypotheses=tuple(lesson.hypotheses),
            alternative_explanations=tuple(lesson.alternative_explanations),
            proposed_experiments=tuple(lesson.proposed_experiments),
            scope=lesson.scope,
        )
    elif isinstance(lesson, Lesson):
        missing["next_proposal"] = "the lesson proposes no hypothesis or experiment"
    else:
        missing["next_proposal"] = _absent(loaded, ArtifactType.LESSON, "lesson")

    return ImprovementTrace(
        episode_id=episode_id,
        topic=topic,
        tool_calls=traced_calls,
        unresolved_citations=unresolved,
        topic_selection=topic_selection,
        intended_improvement=intended,
        reflected_in_video=reflected,
        quality_review=quality,
        publication=publication,
        result=result,
        next_proposal=next_proposal,
        human_annotations=tuple(
            a.model_dump(mode="json") for a in view.annotations if a.anchor.get("target_ref")
        ),
        missing=missing,
    )
