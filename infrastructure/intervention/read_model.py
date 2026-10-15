"""Episode の読み取りモデル（JP-MAS-001 §8 / §12、ADR-0041、``docs/domain/intervention.md``）。

**何も書かない。** ``episode_controls`` 行の無い Episode（古い Episode・日次）は既定値
（legacy / running、``persisted=false``）で読み、採用 manifest が無ければ現行
（``superseded_at IS NULL``）から同じ形を組む（``persisted=false``）。

- stale: 採用 manifest の member が、manifest の採用していない上流 revision から作られている
  （成果物 JSON の参照。``rebound_to`` の辺があればその上流で置き換える / ADR-0041 §6）。
  approval は結び付いた manifest が採用中でなければ stale（INV-45、``approval_is_current``）。
- コメント: 対象 revision が採用 manifest に無ければ「古い版のコメント」として示す。アンカーは
  動かさない（設計 §8）。
- command: ``RECEIVED`` を完了として示さない（``done`` は ``APPLIED`` のときだけ）。
- 費用: 予約台帳と research_calls の見積り（USD）。見積りの無い呼び出し（Codex 等）は
  ``unmeasured`` として別に数え、0 として足さない。LLM の費用は ``unmeasured``。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifacts import parse_script_artifact
from contracts.intervention import (
    TERMINAL_COMMAND_STATUSES,
    AnnotationKind,
    CommandStatus,
    RevisionLinkKind,
)
from contracts.research import ResearchArtifactType, ResearchCallStatus
from contracts.states import ArtifactType, ReservationStatus
from infrastructure.db.models import (
    ArtifactMetadataRow,
    EpisodeRow,
    ProviderReservationRow,
    ResearchArtifactRow,
    ResearchCallRow,
    ResearchRequestRow,
)
from infrastructure.intervention.gate import approval_matches
from infrastructure.intervention.records import (
    derive_links,
    derive_topic_decision,
    json_upstream_refs,
)
from infrastructure.intervention.store import (
    Annotation,
    EpisodeControl,
    InterventionStore,
    manifest_slot,
)
from infrastructure.storage.artifact_store import ArtifactStore

READ_MODEL_SCHEMA_VERSION = "1.0"
#: LLM（Codex）の費用は記録が無い。0 と区別して示す（intervention.md §9）
LLM_COST_UNMEASURED = "unmeasured"


def approval_is_current(annotation: Annotation, control: EpisodeControl) -> bool:
    """approval が**今の**採用 manifest に結び付いているか（INV-45）。

    manifest が変われば（台本・素材・完成品のどれかが替われば新しい manifest になる）旧 manifest の
    approval は現行の合格にならない。承認・投稿の判断はこの述語を通す。approval 以外は常に False。
    規則の宣言元は ``gate.approval_matches``（control gate と同じ規則。AGENTS.md §8）。
    """
    return approval_matches(annotation, control.active_manifest_id)


# --------------------------------------------------------------------------- view


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ControlView(_View):
    mode: str
    control_state: str
    control_revision: int
    policy_version: str
    review_points: list[str]
    active_manifest_id: str | None
    waiting_point: str | None
    waiting_manifest_id: str | None
    hold_requested_at: str | None
    held_at: str | None
    #: False = 行が無く既定値で読んだ（古い Episode・日次）
    persisted: bool


class UpstreamView(_View):
    revision_id: str
    slot: str | None
    #: ``json_ref``（成果物 JSON の参照）/ ``rebound_to``（結び直し）
    source: str
    adopted_revision_id: str | None


class MemberView(_View):
    slot: str
    revision_id: str
    artifact_type: str | None
    scene_id: str | None
    version: int | None
    schema_version: str | None
    bucket: str | None
    object_key: str | None
    sha256: str | None
    created_at: str | None
    #: ``artifact_metadata`` の現行（superseded_at IS NULL）か
    is_current: bool
    readable: bool
    upstream: list[UpstreamView]
    stale: bool
    stale_reasons: list[str]


class ManifestView(_View):
    manifest_id: str | None
    seq: int | None
    parent_manifest_id: str | None
    causation_command_id: str | None
    created_at: str | None
    #: False = 採用 manifest が無く、現行の成果物から組んだ
    persisted: bool
    #: 採用 manifest と現行（superseded_at IS NULL）が食い違う（旧経路の工程が成果物を作った等）
    diverges_from_current: bool
    members: list[MemberView]


class DecisionView(_View):
    decision_id: str | None
    decision_type: str
    actor_type: str
    actor_id: str
    agent_run_id: str | None
    topic_plan_id: str | None
    result_revision_id: str | None
    command_id: str | None
    payload: dict[str, Any]
    created_at: str | None
    #: 単一 Scene を対象にする判断だけ。Activity の外で作った行の workflow は None（migration 0018）
    scene_id: str | None
    workflow_id: str | None
    workflow_run_id: str | None
    #: True = 保存されておらず、読み取り時に保存済みデータから導いた（古い plan の choose_topic）
    derived: bool


class AgentRunView(_View):
    agent_run_id: str
    role: str
    step: str
    model: str | None
    prompt_version: str | None
    snapshot_object_key: str
    snapshot_sha256: str
    request_object_key: str | None
    response_object_key: str | None
    tool_invocations: list[dict[str, Any]]
    token_usage: dict[str, Any] | None
    output_revision_id: str | None
    outcome: str
    created_at: str
    #: その実行を作った Activity の Temporal 識別子。Activity の外の行は None（migration 0018）
    workflow_id: str | None
    workflow_run_id: str | None
    activity_id: str | None


class AnnotationView(_View):
    annotation_id: str
    kind: str
    anchor: dict[str, Any]
    body: str
    payload: dict[str, Any] | None
    actor_type: str
    actor_id: str
    manifest_id: str | None
    refers_to_annotation_id: str | None
    agent_run_id: str | None
    created_at: str
    #: ``open`` / ``resolved``（resolution 注記が指している）
    status: str
    resolved_by: str | None
    #: 対象 revision・manifest が採用中でない（古い版へのコメント。アンカーは動かさない）
    old_version: bool
    #: approval だけ。採用中の manifest に結び付いているか（INV-45）
    approval_current: bool | None


class ChangeSetView(_View):
    change_set_id: str
    kind: str
    status: str
    base_revision_id: str
    base_manifest_id: str | None
    proposal: dict[str, Any]
    impact: dict[str, Any] | None
    estimated_cost_usd: str | None
    rationale: str | None
    variant_group: str | None
    applied_revision_id: str | None
    applied_manifest_id: str | None
    proposed_by_command_id: str
    decided_by_command_id: str | None
    actor_type: str
    actor_id: str
    created_at: str


class CommandView(_View):
    command_id: str
    type: str
    status: str
    #: ``APPLIED`` のときだけ True。``RECEIVED`` を完了として示さない
    done: bool
    display_status: str
    actor_type: str
    actor_id: str
    granted_scope: str
    reject_reason: str | None
    result: dict[str, Any] | None
    received_at: str
    finished_at: str | None


class ReservationView(_View):
    reservation_id: str
    provider: str
    status: str
    scene_id: str | None
    round: int
    dispatched_at: str | None
    provider_job_ref_present: bool
    estimated_cost_usd: str | None


class OpenReservations(_View):
    #: dispatch 済み・未精算・provider の job 参照あり（完了・課金されうる）
    in_flight: list[ReservationView]
    #: dispatch 済み・未精算・job 参照なし（送ったかどうか・結果が分からない。自動で再送しない）
    unknown_outcome: list[ReservationView]
    #: 予約済み・未送信（課金されない。resume 後に同じ冪等キーで再利用される）
    undispatched: list[ReservationView]


class UnmeasuredCalls(_View):
    provider: str
    calls: int


class CostView(_View):
    #: 予約台帳の見積り（reserved + spent。abandoned は数えない）
    provider_usd: str
    #: research_calls の見積り（同上）
    research_usd: str
    measured_usd: str
    #: 見積りの記録が無い呼び出し（0 として足さない）
    unmeasured: list[UnmeasuredCalls]
    llm: Literal["unmeasured"] = LLM_COST_UNMEASURED


class EventView(_View):
    seq: int
    event_name: str
    actor_type: str
    actor_id: str
    refs: dict[str, Any]
    command_id: str | None
    created_at: str


class LinkView(_View):
    upstream_revision_id: str
    downstream_revision_id: str
    kind: str


class ScriptSegmentView(_View):
    segment_id: str
    narration: str
    visual: str
    duration_ms: int


class ScriptView(_View):
    """採用 manifest の台本の本文（ReviewPacket・レビュアーが読む）。"""

    revision_id: str
    title: str
    hook: str
    language: str
    total_duration_ms: int
    segments: list[ScriptSegmentView]


class EvidenceView(_View):
    """Episode を参照する research の成果物（参照だけ。research は所有者のまま / INV-37）。"""

    research_artifact_id: str
    artifact_type: str
    request_id: str
    bucket: str
    object_key: str
    sha256: str
    is_current: bool
    #: ``research_script_verification`` だけ（読めなければ None）
    verdict: str | None
    #: 照合した台本の revision（``script_ref.artifact_id``）
    script_revision_id: str | None


class EpisodeView(_View):
    schema_version: str = READ_MODEL_SCHEMA_VERSION
    episode_id: str
    #: 既存 ``episodes.status``（phase）。制御状態は ``control``
    phase: str
    topic: str | None
    topic_plan_id: str | None
    blocked_reason: str | None
    control: ControlView
    manifest: ManifestView
    decisions: list[DecisionView]
    agent_runs: list[AgentRunView]
    annotations: list[AnnotationView]
    #: 採用中でない manifest に結び付いた approval（INV-45。流用しない）
    stale_approvals: list[str]
    change_sets: list[ChangeSetView]
    commands: list[CommandView]
    reservations: OpenReservations
    costs: CostView
    events: list[EventView]
    #: ``revision_links`` に記録された辺
    recorded_links: list[LinkView]
    #: 成果物 JSON の参照から導いた辺（古い Episode も。書かない）
    derived_links: list[LinkView]
    #: 本体を読めなかった revision
    unreadable_revisions: list[str]
    #: 採用 manifest の台本（無い・読めなければ None）
    script: ScriptView | None = None
    evidence: list[EvidenceView] = []


class FieldChange(_View):
    path: str
    before: Any = None
    after: Any = None


class RevisionComparison(_View):
    a_revision_id: str
    b_revision_id: str
    artifact_type: str
    #: ``script``（segment ごと）/ ``storyboard``（scene ごと）/ ``generic``（JSON）
    kind: str
    changes: list[FieldChange]


# --------------------------------------------------------------------------- 読み取り


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _usd(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


class EpisodeReadModel:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_store: ArtifactStore,
    ) -> None:
        self._session_factory = session_factory
        self._store = artifact_store

    async def load(self, episode_id: str) -> EpisodeView | None:
        """Episode の全体像。Episode が無ければ ``None``。何も書かない。"""
        async with self._session_factory() as session:
            episode = await session.get(EpisodeRow, uuid.UUID(str(episode_id)))
            if episode is None:
                return None
            store = InterventionStore(session)
            control = await store.get_control(episode_id)
            artifacts = {
                str(r.id): r
                for r in await session.scalars(
                    select(ArtifactMetadataRow).where(ArtifactMetadataRow.episode_id == episode.id)
                )
            }
            manifest = (
                None
                if control.active_manifest_id is None
                else await store.get_manifest(control.active_manifest_id)
            )
            current = {
                manifest_slot(r.artifact_type, r.scene_id): str(r.id)
                for r in artifacts.values()
                if r.superseded_at is None
            }
            members_map = current if manifest is None else manifest.members
            links = await store.links(episode_id=episode_id)
            members = await self._members(members_map, artifacts, links)
            annotations = await store.list_annotations(episode_id)
            decisions = await self._decisions(session, store, episode_id, episode.topic_plan_id)
            derived = await derive_links(session, self._store, str(episode_id))
            view = EpisodeView(
                episode_id=str(episode.id),
                phase=episode.status,
                topic=episode.topic,
                topic_plan_id=None if episode.topic_plan_id is None else str(episode.topic_plan_id),
                blocked_reason=episode.blocked_reason,
                control=_control_view(control),
                manifest=ManifestView(
                    manifest_id=None if manifest is None else manifest.manifest_id,
                    seq=None if manifest is None else manifest.seq,
                    parent_manifest_id=None if manifest is None else manifest.parent_manifest_id,
                    causation_command_id=(
                        None if manifest is None else manifest.causation_command_id
                    ),
                    created_at=None if manifest is None else _iso(manifest.created_at),
                    persisted=manifest is not None,
                    diverges_from_current=manifest is not None and manifest.members != current,
                    members=members,
                ),
                decisions=decisions,
                agent_runs=[
                    AgentRunView(
                        agent_run_id=r.agent_run_id,
                        role=r.role.value,
                        step=r.step,
                        model=r.model,
                        prompt_version=r.prompt_version,
                        snapshot_object_key=r.snapshot_object_key,
                        snapshot_sha256=r.snapshot_sha256,
                        request_object_key=r.request_object_key,
                        response_object_key=r.response_object_key,
                        tool_invocations=list(r.tool_invocations),
                        token_usage=r.token_usage,
                        output_revision_id=r.output_revision_id,
                        outcome=r.outcome.value,
                        created_at=r.created_at.isoformat(),
                        workflow_id=r.workflow_id,
                        workflow_run_id=r.workflow_run_id,
                        activity_id=r.activity_id,
                    )
                    for r in await store.list_agent_runs(episode_id=episode_id)
                ],
                annotations=_annotations(annotations, control, set(members_map.values())),
                stale_approvals=[
                    a.annotation_id
                    for a in annotations
                    if a.kind is AnnotationKind.APPROVAL
                    and not approval_is_current(a, control)
                    and a.annotation_id not in _resolved(annotations)
                ],
                change_sets=[
                    ChangeSetView(
                        change_set_id=c.change_set_id,
                        kind=c.kind.value,
                        status=c.status.value,
                        base_revision_id=c.base_revision_id,
                        base_manifest_id=c.base_manifest_id,
                        proposal=c.proposal,
                        impact=c.impact,
                        estimated_cost_usd=(
                            None if c.estimated_cost_usd is None else _usd(c.estimated_cost_usd)
                        ),
                        rationale=c.rationale,
                        variant_group=c.variant_group,
                        applied_revision_id=c.applied_revision_id,
                        applied_manifest_id=c.applied_manifest_id,
                        proposed_by_command_id=c.proposed_by_command_id,
                        decided_by_command_id=c.decided_by_command_id,
                        actor_type=c.actor.type.value,
                        actor_id=c.actor.id,
                        created_at=c.created_at.isoformat(),
                    )
                    for c in await store.list_change_sets(episode_id)
                ],
                commands=[
                    CommandView(
                        command_id=c.command_id,
                        type=c.type.value,
                        status=c.status.value,
                        done=c.status is CommandStatus.APPLIED,
                        display_status=_display_status(c.status),
                        actor_type=c.actor.type.value,
                        actor_id=c.actor.id,
                        granted_scope=c.granted_scope.value,
                        reject_reason=None if c.reject_reason is None else c.reject_reason.value,
                        result=c.result,
                        received_at=c.received_at.isoformat(),
                        finished_at=_iso(c.finished_at),
                    )
                    for c in await store.list_commands(episode_id)
                ],
                reservations=await _open_reservations(session, episode.id),
                costs=await _costs(session, episode.id),
                events=[
                    EventView(
                        seq=e.seq,
                        event_name=e.event_name.value,
                        actor_type=e.actor.type.value,
                        actor_id=e.actor.id,
                        refs=e.refs,
                        command_id=e.command_id,
                        created_at=e.created_at.isoformat(),
                    )
                    for e in await store.list_events(episode_id)
                ],
                recorded_links=[
                    LinkView(
                        upstream_revision_id=lk.upstream_revision_id,
                        downstream_revision_id=lk.downstream_revision_id,
                        kind=lk.kind.value,
                    )
                    for lk in links
                ],
                derived_links=[
                    LinkView(
                        upstream_revision_id=e.upstream_revision_id,
                        downstream_revision_id=e.downstream_revision_id,
                        kind=e.kind.value,
                    )
                    for e in derived.edges
                ],
                unreadable_revisions=list(derived.unreadable),
                script=await self._script(members_map.get("script"), artifacts),
                evidence=await self._evidence(session, episode.id),
            )
            # 読み取りだけ。get_control 等が始めた transaction は書かずに閉じる
            await session.rollback()
            return view

    async def _members(
        self,
        members_map: Mapping[str, str],
        artifacts: Mapping[str, ArtifactMetadataRow],
        links: list[Any],
    ) -> list[MemberView]:
        rebound: dict[str, list[str]] = {}
        for link in links:
            if link.kind is RevisionLinkKind.REBOUND_TO:
                rebound.setdefault(link.downstream_revision_id, []).append(
                    link.upstream_revision_id
                )

        def slot_of(revision_id: str) -> str | None:
            row = artifacts.get(revision_id)
            return None if row is None else manifest_slot(row.artifact_type, row.scene_id)

        views: list[MemberView] = []
        for slot, revision_id in sorted(members_map.items()):
            row = artifacts.get(revision_id)
            refs: list[str] = []
            readable = row is not None
            if row is not None:
                try:
                    refs = json_upstream_refs(await self._store.get_json(row.object_key))
                except Exception:  # noqa: BLE001 - 読めないことを view に出す
                    readable = False
            # 結び直しは同じ slot の JSON 参照を置き換える（ADR-0041 §6）
            upstream: dict[str | None, tuple[str, str]] = {}
            for ref in refs:
                upstream.setdefault(slot_of(ref), (ref, "json_ref"))
            for ref in rebound.get(revision_id, []):
                upstream[slot_of(ref)] = (ref, "rebound_to")
            upstream_views: list[UpstreamView] = []
            reasons: list[str] = []
            for up_slot, (ref, source) in upstream.items():
                adopted = None if up_slot is None else members_map.get(up_slot)
                upstream_views.append(
                    UpstreamView(
                        revision_id=ref, slot=up_slot, source=source, adopted_revision_id=adopted
                    )
                )
                if adopted is not None and adopted != ref:
                    reasons.append(f"{up_slot}: built from {ref}, adopted is {adopted}")
            views.append(
                MemberView(
                    slot=slot,
                    revision_id=revision_id,
                    artifact_type=None if row is None else row.artifact_type,
                    scene_id=None if row is None else row.scene_id,
                    version=None if row is None else row.version,
                    schema_version=None if row is None else row.schema_version,
                    bucket=None if row is None else row.bucket,
                    object_key=None if row is None else row.object_key,
                    sha256=None if row is None else row.sha256,
                    created_at=None if row is None else _iso(row.created_at),
                    is_current=row is not None and row.superseded_at is None,
                    readable=readable,
                    upstream=upstream_views,
                    stale=bool(reasons),
                    stale_reasons=reasons,
                )
            )
        return views

    async def _script(
        self, revision_id: str | None, artifacts: Mapping[str, ArtifactMetadataRow]
    ) -> ScriptView | None:
        row = None if revision_id is None else artifacts.get(revision_id)
        if row is None:
            return None
        try:
            script = parse_script_artifact(await self._store.get_json(row.object_key))
        except Exception:  # noqa: BLE001 - 読めない台本は None（ReviewPacket が欠落として示す）
            return None
        return ScriptView(
            revision_id=str(row.id),
            title=script.title,
            hook=script.hook,
            language=script.language,
            total_duration_ms=script.total_duration_ms,
            segments=[
                ScriptSegmentView(
                    segment_id=s.id,
                    narration=s.narration,
                    visual=s.visual,
                    duration_ms=s.duration_ms,
                )
                for s in script.scenes
            ],
        )

    async def _evidence(self, session: AsyncSession, episode_id: uuid.UUID) -> list[EvidenceView]:
        rows = await session.execute(
            select(ResearchArtifactRow)
            .join(ResearchRequestRow, ResearchRequestRow.id == ResearchArtifactRow.request_id)
            .where(ResearchRequestRow.episode_id == episode_id)
            .order_by(ResearchArtifactRow.created_at, ResearchArtifactRow.id)
        )
        views = []
        for (row,) in rows:
            verdict = script_revision = None
            if row.artifact_type == ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION.value:
                try:
                    body = await self._store.get_json(row.object_key)
                    verdict = body.get("verdict")
                    ref = body.get("script_ref") or {}
                    script_revision = ref.get("artifact_id")
                except Exception:  # noqa: BLE001 - 読めなければ verdict は None
                    pass
            views.append(
                EvidenceView(
                    research_artifact_id=str(row.id),
                    artifact_type=row.artifact_type,
                    request_id=str(row.request_id),
                    bucket=row.bucket,
                    object_key=row.object_key,
                    sha256=row.sha256,
                    is_current=row.superseded_at is None,
                    verdict=None if verdict is None else str(verdict),
                    script_revision_id=None if script_revision is None else str(script_revision),
                )
            )
        return views

    @staticmethod
    async def _decisions(
        session: AsyncSession,
        store: InterventionStore,
        episode_id: str,
        topic_plan_id: uuid.UUID | None,
    ) -> list[DecisionView]:
        records = list(await store.list_decisions(episode_id=episode_id))
        if topic_plan_id is not None:
            records = list(await store.list_decisions(topic_plan_id=topic_plan_id)) + records
        views = [
            DecisionView(
                decision_id=d.decision_id,
                decision_type=d.decision_type,
                actor_type=d.actor.type.value,
                actor_id=d.actor.id,
                agent_run_id=d.agent_run_id,
                topic_plan_id=d.topic_plan_id,
                result_revision_id=d.result_revision_id,
                command_id=d.command_id,
                payload=d.payload,
                created_at=d.created_at.isoformat(),
                scene_id=d.scene_id,
                workflow_id=d.workflow_id,
                workflow_run_id=d.workflow_run_id,
                derived=False,
            )
            for d in records
        ]
        has_topic_decision = any(v.decision_type == "choose_topic" for v in views)
        if topic_plan_id is not None and not has_topic_decision:
            # 記録の無い古い plan: 保存済み候補から導く（書かない。derived=True で区別）
            payload = await derive_topic_decision(session, str(topic_plan_id))
            if payload is not None:
                views.insert(
                    0,
                    DecisionView(
                        decision_id=None,
                        decision_type=payload.decision_type,
                        actor_type="agent",
                        actor_id="producer",
                        agent_run_id=None,
                        topic_plan_id=str(topic_plan_id),
                        result_revision_id=None,
                        command_id=None,
                        payload=payload.model_dump(mode="json"),
                        created_at=None,
                        scene_id=None,
                        workflow_id=None,
                        workflow_run_id=None,
                        derived=True,
                    ),
                )
        return views

    async def compare_revisions(self, a_id: str, b_id: str) -> RevisionComparison:
        """2つの revision の差分。同じ型でなければ ``ValueError``。"""
        async with self._session_factory() as session:
            a = await session.get(ArtifactMetadataRow, uuid.UUID(str(a_id)))
            b = await session.get(ArtifactMetadataRow, uuid.UUID(str(b_id)))
        if a is None or b is None:
            raise ValueError("revision not found")
        if a.artifact_type != b.artifact_type:
            raise ValueError(f"cannot compare {a.artifact_type} with {b.artifact_type}")
        before = await self._store.get_json(a.object_key)
        after = await self._store.get_json(b.object_key)
        if a.artifact_type == ArtifactType.SCRIPT.value:
            kind, changes = "script", _keyed_diff(before, after, "scenes", "id", "segments")
        elif a.artifact_type == ArtifactType.STORYBOARD.value:
            kind, changes = "storyboard", _keyed_diff(before, after, "scenes", "scene_id", "scenes")
        else:
            kind, changes = "generic", _json_diff(before, after, "")
        return RevisionComparison(
            a_revision_id=str(a.id),
            b_revision_id=str(b.id),
            artifact_type=a.artifact_type,
            kind=kind,
            changes=changes,
        )


# --------------------------------------------------------------------------- 部品


def _control_view(control: EpisodeControl) -> ControlView:
    return ControlView(
        mode=control.mode.value,
        control_state=control.control_state.value,
        control_revision=control.control_revision,
        policy_version=control.policy_version,
        review_points=[p.value for p in control.review_points],
        active_manifest_id=control.active_manifest_id,
        waiting_point=None if control.waiting_point is None else control.waiting_point.value,
        waiting_manifest_id=control.waiting_manifest_id,
        hold_requested_at=_iso(control.hold_requested_at),
        held_at=_iso(control.held_at),
        persisted=control.persisted,
    )


def _resolved(annotations: list[Annotation]) -> dict[str, str]:
    """解決された注記 id → 解決した注記 id。"""
    return {
        a.refers_to_annotation_id: a.annotation_id
        for a in annotations
        if a.kind is AnnotationKind.RESOLUTION and a.refers_to_annotation_id is not None
    }


def _annotations(
    annotations: list[Annotation], control: EpisodeControl, adopted: set[str]
) -> list[AnnotationView]:
    resolved = _resolved(annotations)
    views = []
    for a in annotations:
        revision = a.anchor.get("revision_id")
        anchored_manifest = a.anchor.get("manifest_id") or a.manifest_id
        if revision is not None:
            old = revision not in adopted
        elif anchored_manifest is not None and a.kind is not AnnotationKind.RESOLUTION:
            old = anchored_manifest != control.active_manifest_id
        else:
            old = False
        views.append(
            AnnotationView(
                annotation_id=a.annotation_id,
                kind=a.kind.value,
                anchor=dict(a.anchor),
                body=a.body,
                payload=a.payload,
                actor_type=a.actor.type.value,
                actor_id=a.actor.id,
                manifest_id=a.manifest_id,
                refers_to_annotation_id=a.refers_to_annotation_id,
                agent_run_id=a.agent_run_id,
                created_at=a.created_at.isoformat(),
                status="resolved" if a.annotation_id in resolved else "open",
                resolved_by=resolved.get(a.annotation_id),
                old_version=old,
                approval_current=(
                    approval_is_current(a, control) if a.kind is AnnotationKind.APPROVAL else None
                ),
            )
        )
    return views


def _display_status(status: CommandStatus) -> str:
    if status is CommandStatus.APPLIED:
        return "applied"
    if status in TERMINAL_COMMAND_STATUSES:
        return f"{status.value} (not applied)"
    return f"{status.value} (accepted, not applied yet)"


def _reservation_view(row: ProviderReservationRow) -> ReservationView:
    return ReservationView(
        reservation_id=str(row.id),
        provider=row.provider,
        status=row.status,
        scene_id=row.scene_id,
        round=row.round,
        dispatched_at=_iso(row.dispatched_at),
        provider_job_ref_present=row.provider_job_ref is not None,
        estimated_cost_usd=None if row.estimated_cost_usd is None else _usd(row.estimated_cost_usd),
    )


async def _open_reservations(session: AsyncSession, episode_id: uuid.UUID) -> OpenReservations:
    rows = list(
        await session.scalars(
            select(ProviderReservationRow)
            .where(
                ProviderReservationRow.episode_id == episode_id,
                ProviderReservationRow.status == ReservationStatus.RESERVED.value,
            )
            .order_by(ProviderReservationRow.reserved_at, ProviderReservationRow.id)
        )
    )
    return OpenReservations(
        in_flight=[
            _reservation_view(r)
            for r in rows
            if r.dispatched_at is not None and r.provider_job_ref is not None
        ],
        unknown_outcome=[
            _reservation_view(r)
            for r in rows
            if r.dispatched_at is not None and r.provider_job_ref is None
        ],
        undispatched=[_reservation_view(r) for r in rows if r.dispatched_at is None],
    )


async def _costs(session: AsyncSession, episode_id: uuid.UUID) -> CostView:
    provider_total = Decimal("0")
    research_total = Decimal("0")
    unmeasured: Counter[str] = Counter()
    for row in await session.scalars(
        select(ProviderReservationRow).where(
            ProviderReservationRow.episode_id == episode_id,
            ProviderReservationRow.status != ReservationStatus.ABANDONED.value,
        )
    ):
        if row.estimated_cost_usd is None:
            unmeasured[row.provider] += 1
        else:
            provider_total += row.estimated_cost_usd
    for call in await session.scalars(
        select(ResearchCallRow)
        .join(ResearchRequestRow, ResearchRequestRow.id == ResearchCallRow.request_id)
        .where(
            ResearchRequestRow.episode_id == episode_id,
            ResearchCallRow.status != ResearchCallStatus.ABANDONED.value,
        )
    ):
        if call.estimated_cost_usd is None:
            unmeasured[f"research:{call.provider_call}"] += 1
        else:
            research_total += call.estimated_cost_usd
    return CostView(
        provider_usd=_usd(provider_total),
        research_usd=_usd(research_total),
        measured_usd=_usd(provider_total + research_total),
        unmeasured=[UnmeasuredCalls(provider=p, calls=n) for p, n in sorted(unmeasured.items())],
    )


def _json_diff(before: Any, after: Any, path: str) -> list[FieldChange]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[FieldChange] = []
        for key in sorted(set(before) | set(after), key=str):
            sub = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append(FieldChange(path=sub, after=after[key]))
            elif key not in after:
                changes.append(FieldChange(path=sub, before=before[key]))
            else:
                changes.extend(_json_diff(before[key], after[key], sub))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        for index in range(max(len(before), len(after))):
            sub = f"{path}[{index}]"
            if index >= len(before):
                changes.append(FieldChange(path=sub, after=after[index]))
            elif index >= len(after):
                changes.append(FieldChange(path=sub, before=before[index]))
            else:
                changes.extend(_json_diff(before[index], after[index], sub))
        return changes
    return [] if before == after else [FieldChange(path=path, before=before, after=after)]


def _keyed_diff(
    before: Mapping[str, Any], after: Mapping[str, Any], list_key: str, id_key: str, label: str
) -> list[FieldChange]:
    """``list_key`` の要素を ``id_key`` で突き合わせて比べる（segment / scene ごと）。"""
    changes = [
        c
        for c in _json_diff(
            {k: v for k, v in before.items() if k != list_key},
            {k: v for k, v in after.items() if k != list_key},
            "",
        )
    ]
    a_items = {str(i.get(id_key)): i for i in before.get(list_key, []) if isinstance(i, Mapping)}
    b_items = {str(i.get(id_key)): i for i in after.get(list_key, []) if isinstance(i, Mapping)}
    order = list(a_items) + [k for k in b_items if k not in a_items]
    for item_id in order:
        sub = f"{label}[{item_id}]"
        if item_id not in b_items:
            changes.append(FieldChange(path=sub, before=a_items[item_id]))
        elif item_id not in a_items:
            changes.append(FieldChange(path=sub, after=b_items[item_id]))
        else:
            changes.extend(_json_diff(a_items[item_id], b_items[item_id], sub))
    a_order = [k for k in a_items if k in b_items]
    b_order = [k for k in b_items if k in a_items]
    if a_order != b_order:
        changes.append(FieldChange(path=f"{label}.order", before=a_order, after=b_order))
    return changes


__all__ = [
    "LLM_COST_UNMEASURED",
    "READ_MODEL_SCHEMA_VERSION",
    "EpisodeReadModel",
    "EpisodeView",
    "RevisionComparison",
    "approval_is_current",
]
