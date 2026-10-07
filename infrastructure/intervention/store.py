"""判断記録と介入の表への低水準アクセス（ADR-0041、``docs/domain/intervention.md``）。

全担当が共有する唯一の永続化の入口。**commit は呼び出し側**（既存リポジトリと同じ）。
追記型の表（decision_records / annotations / episode_events / artifact_manifests / revision_links /
agent_runs）には更新・削除の関数を作らない（INV-41）。

Episode 内の直列化: 制御・採用・イベント採番を変える書き手は ``lock_control`` で
``episode_controls`` 行を ``FOR UPDATE`` で取ってから書く。dispatch gate は同じ行を
``FOR SHARE`` で読む（PostgreSQL。SQLite は DB 全体の書き込み直列なので lock 句は無視される）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.intervention import (
    DEFAULT_CONTROL_STATE,
    DEFAULT_MODE,
    INTERVENTION_POLICY_VERSION,
    INTERVENTION_SCHEMA_VERSION,
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    ChangeKind,
    ChangeSetStatus,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    InterventionMode,
    RejectReason,
    ReviewPoint,
    RevisionLinkKind,
    Scope,
)
from infrastructure.db.models import (
    AgentRunRow,
    AnnotationRow,
    ArtifactManifestRow,
    ChangeSetRow,
    DecisionRecordRow,
    EpisodeControlRow,
    EpisodeEventRow,
    InterventionCommandRow,
    RevisionLinkRow,
)


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _opt_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Actor:
    type: ActorType
    id: str


SYSTEM_ACTOR = Actor(ActorType.SYSTEM, "system")


# --------------------------------------------------------------------------- 値


@dataclass(frozen=True, slots=True)
class EpisodeControl:
    episode_id: str
    mode: InterventionMode
    control_state: ControlState
    control_revision: int
    policy_version: str
    review_points: tuple[ReviewPoint, ...]
    active_manifest_id: str | None
    waiting_point: ReviewPoint | None
    waiting_manifest_id: str | None
    last_event_seq: int
    hold_requested_at: datetime | None
    held_at: datetime | None
    #: 行が DB にあるか（False = 古い Episode・日次実行の既定値として読んだ）
    persisted: bool


def default_control(episode_id: str) -> EpisodeControl:
    return EpisodeControl(
        episode_id=str(episode_id),
        mode=DEFAULT_MODE,
        control_state=DEFAULT_CONTROL_STATE,
        control_revision=0,
        policy_version=INTERVENTION_POLICY_VERSION,
        review_points=(),
        active_manifest_id=None,
        waiting_point=None,
        waiting_manifest_id=None,
        last_event_seq=0,
        hold_requested_at=None,
        held_at=None,
        persisted=False,
    )


def _to_control(row: EpisodeControlRow) -> EpisodeControl:
    return EpisodeControl(
        episode_id=str(row.episode_id),
        mode=InterventionMode(row.mode),
        control_state=ControlState(row.control_state),
        control_revision=row.control_revision,
        policy_version=row.policy_version,
        review_points=tuple(ReviewPoint(p) for p in (row.review_points or [])),
        active_manifest_id=None if row.active_manifest_id is None else str(row.active_manifest_id),
        waiting_point=None if row.waiting_point is None else ReviewPoint(row.waiting_point),
        waiting_manifest_id=(
            None if row.waiting_manifest_id is None else str(row.waiting_manifest_id)
        ),
        last_event_seq=row.last_event_seq,
        hold_requested_at=row.hold_requested_at,
        held_at=row.held_at,
        persisted=True,
    )


@dataclass(frozen=True, slots=True)
class Command:
    command_id: str
    episode_id: str
    type: CommandType
    status: CommandStatus
    actor: Actor
    granted_scope: Scope
    expected_control_revision: int | None
    expected_target_revision: str | None
    payload: dict[str, Any]
    payload_sha256: str
    result: dict[str, Any] | None
    reject_reason: RejectReason | None
    correlation_id: str | None
    received_at: datetime
    finished_at: datetime | None
    dispatched_at: datetime | None
    dispatch_attempts: int


def _to_command(row: InterventionCommandRow) -> Command:
    return Command(
        command_id=row.id,
        episode_id=str(row.episode_id),
        type=CommandType(row.type),
        status=CommandStatus(row.status),
        actor=Actor(ActorType(row.actor_type), row.actor_id),
        granted_scope=Scope(row.granted_scope),
        expected_control_revision=row.expected_control_revision,
        expected_target_revision=row.expected_target_revision,
        payload=dict(row.payload),
        payload_sha256=row.payload_sha256,
        result=None if row.result is None else dict(row.result),
        reject_reason=None if row.reject_reason is None else RejectReason(row.reject_reason),
        correlation_id=row.correlation_id,
        received_at=row.received_at,
        finished_at=row.finished_at,
        dispatched_at=row.dispatched_at,
        dispatch_attempts=row.dispatch_attempts,
    )


@dataclass(frozen=True, slots=True)
class Manifest:
    manifest_id: str
    episode_id: str
    seq: int
    parent_manifest_id: str | None
    #: slot → revision_id（slot は ``manifest_slot`` の規約）
    members: dict[str, str]
    causation_command_id: str | None
    actor: Actor
    created_at: datetime


def manifest_slot(artifact_type: str, scene_id: str | None = None) -> str:
    """manifest の slot 名。シーン単位の型は ``{type}:{scene_id}``。"""
    return artifact_type if scene_id is None else f"{artifact_type}:{scene_id}"


def _to_manifest(row: ArtifactManifestRow) -> Manifest:
    return Manifest(
        manifest_id=str(row.id),
        episode_id=str(row.episode_id),
        seq=row.seq,
        parent_manifest_id=None if row.parent_manifest_id is None else str(row.parent_manifest_id),
        members={str(k): str(v) for k, v in dict(row.members).items()},
        causation_command_id=row.causation_command_id,
        actor=Actor(ActorType(row.actor_type), row.actor_id),
        created_at=row.created_at,
    )


@dataclass(frozen=True, slots=True)
class EpisodeEvent:
    episode_id: str
    seq: int
    event_name: InterventionEvent
    actor: Actor
    refs: dict[str, Any]
    command_id: str | None
    created_at: datetime
    projected_at: datetime | None


def _to_event(row: EpisodeEventRow) -> EpisodeEvent:
    return EpisodeEvent(
        episode_id=str(row.episode_id),
        seq=row.seq,
        event_name=InterventionEvent(row.event_name),
        actor=Actor(ActorType(row.actor_type), row.actor_id),
        refs=dict(row.refs),
        command_id=row.command_id,
        created_at=row.created_at,
        projected_at=row.projected_at,
    )


@dataclass(frozen=True, slots=True)
class Annotation:
    annotation_id: str
    episode_id: str
    kind: AnnotationKind
    anchor: dict[str, Any]
    body: str
    payload: dict[str, Any] | None
    refers_to_annotation_id: str | None
    manifest_id: str | None
    actor: Actor
    command_id: str | None
    agent_run_id: str | None
    created_at: datetime


def _to_annotation(row: AnnotationRow) -> Annotation:
    return Annotation(
        annotation_id=str(row.id),
        episode_id=str(row.episode_id),
        kind=AnnotationKind(row.kind),
        anchor=dict(row.anchor),
        body=row.body,
        payload=None if row.payload is None else dict(row.payload),
        refers_to_annotation_id=(
            None if row.refers_to_annotation_id is None else str(row.refers_to_annotation_id)
        ),
        manifest_id=None if row.manifest_id is None else str(row.manifest_id),
        actor=Actor(ActorType(row.actor_type), row.actor_id),
        command_id=row.command_id,
        agent_run_id=None if row.agent_run_id is None else str(row.agent_run_id),
        created_at=row.created_at,
    )


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_set_id: str
    episode_id: str
    kind: ChangeKind
    status: ChangeSetStatus
    base_revision_id: str
    base_manifest_id: str | None
    proposal: dict[str, Any]
    impact: dict[str, Any] | None
    estimated_cost_usd: Decimal | None
    rationale: str | None
    variant_group: str | None
    applied_revision_id: str | None
    applied_manifest_id: str | None
    proposed_by_command_id: str
    decided_by_command_id: str | None
    actor: Actor
    created_at: datetime


def _to_change_set(row: ChangeSetRow) -> ChangeSet:
    return ChangeSet(
        change_set_id=str(row.id),
        episode_id=str(row.episode_id),
        kind=ChangeKind(row.kind),
        status=ChangeSetStatus(row.status),
        base_revision_id=str(row.base_revision_id),
        base_manifest_id=None if row.base_manifest_id is None else str(row.base_manifest_id),
        proposal=dict(row.proposal),
        impact=None if row.impact is None else dict(row.impact),
        estimated_cost_usd=row.estimated_cost_usd,
        rationale=row.rationale,
        variant_group=row.variant_group,
        applied_revision_id=(
            None if row.applied_revision_id is None else str(row.applied_revision_id)
        ),
        applied_manifest_id=(
            None if row.applied_manifest_id is None else str(row.applied_manifest_id)
        ),
        proposed_by_command_id=row.proposed_by_command_id,
        decided_by_command_id=row.decided_by_command_id,
        actor=Actor(ActorType(row.actor_type), row.actor_id),
        created_at=row.created_at,
    )


@dataclass(frozen=True, slots=True)
class RevisionLink:
    upstream_revision_id: str
    downstream_revision_id: str
    kind: RevisionLinkKind
    causation_command_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    episode_id: str | None
    topic_plan_id: str | None
    agent_run_id: str | None
    decision_type: str
    actor: Actor
    payload: dict[str, Any]
    result_revision_id: str | None
    command_id: str | None
    correlation_id: str | None
    created_at: datetime


def _to_decision(row: DecisionRecordRow) -> DecisionRecord:
    return DecisionRecord(
        decision_id=str(row.id),
        episode_id=None if row.episode_id is None else str(row.episode_id),
        topic_plan_id=None if row.topic_plan_id is None else str(row.topic_plan_id),
        agent_run_id=None if row.agent_run_id is None else str(row.agent_run_id),
        decision_type=row.decision_type,
        actor=Actor(ActorType(row.actor_type), row.actor_id),
        payload=dict(row.payload),
        result_revision_id=None if row.result_revision_id is None else str(row.result_revision_id),
        command_id=row.command_id,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )


@dataclass(frozen=True, slots=True)
class AgentRun:
    agent_run_id: str
    episode_id: str | None
    topic_plan_id: str | None
    role: AgentRole
    step: str
    model: str | None
    prompt_version: str | None
    snapshot_object_key: str
    snapshot_sha256: str
    request_object_key: str | None
    request_sha256: str | None
    response_object_key: str | None
    response_sha256: str | None
    tool_invocations: list[dict[str, Any]]
    token_usage: dict[str, Any] | None
    estimated_cost_usd: Decimal | None
    output_revision_id: str | None
    outcome: AgentRunOutcome
    error_summary: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


def _to_agent_run(row: AgentRunRow) -> AgentRun:
    return AgentRun(
        agent_run_id=str(row.id),
        episode_id=None if row.episode_id is None else str(row.episode_id),
        topic_plan_id=None if row.topic_plan_id is None else str(row.topic_plan_id),
        role=AgentRole(row.role),
        step=row.step,
        model=row.model,
        prompt_version=row.prompt_version,
        snapshot_object_key=row.snapshot_object_key,
        snapshot_sha256=row.snapshot_sha256,
        request_object_key=row.request_object_key,
        request_sha256=row.request_sha256,
        response_object_key=row.response_object_key,
        response_sha256=row.response_sha256,
        tool_invocations=list(row.tool_invocations or []),
        token_usage=None if row.token_usage is None else dict(row.token_usage),
        estimated_cost_usd=row.estimated_cost_usd,
        output_revision_id=None if row.output_revision_id is None else str(row.output_revision_id),
        outcome=AgentRunOutcome(row.outcome),
        error_summary=row.error_summary,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
    )


@dataclass(slots=True)
class ControlChange:
    """``update_control`` に渡す差分。None の項目は変えない。"""

    mode: InterventionMode | None = None
    control_state: ControlState | None = None
    review_points: tuple[ReviewPoint, ...] | None = None
    active_manifest_id: str | None = None
    #: 待機地点を消すときは ``clear_waiting=True``
    waiting_point: ReviewPoint | None = None
    waiting_manifest_id: str | None = None
    clear_waiting: bool = False
    hold_requested_at: datetime | None = None
    held_at: datetime | None = None
    clear_hold_times: bool = False
    bump_revision: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


class DuplicateCommandError(Exception):
    """同じ command_id が違う payload で来た（INV-42）。"""

    def __init__(self, existing: Command) -> None:
        super().__init__(f"command_id {existing.command_id} was already used with another payload")
        self.existing = existing


# --------------------------------------------------------------------------- store


class InterventionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    # ---------------------------------------------------------------- 制御
    async def get_control(
        self, episode_id: uuid.UUID | str, *, lock: Literal["share", "update"] | None = None
    ) -> EpisodeControl:
        stmt = select(EpisodeControlRow).where(EpisodeControlRow.episode_id == _uuid(episode_id))
        if lock == "share":
            stmt = stmt.with_for_update(read=True)
        elif lock == "update":
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_control(row) if row is not None else default_control(str(episode_id))

    async def lock_control(self, episode_id: uuid.UUID | str) -> EpisodeControl:
        """制御行を ``FOR UPDATE`` で取る。無ければ legacy/running の行を作ってから取る。

        行の作成が並行した場合は UNIQUE(PK) で片方が落ちるので、savepoint で吸収して読み直す。
        """
        current = await self.get_control(episode_id, lock="update")
        if current.persisted:
            return current
        try:
            async with self._session.begin_nested():
                self._session.add(
                    EpisodeControlRow(
                        episode_id=_uuid(episode_id),
                        mode=DEFAULT_MODE.value,
                        control_state=DEFAULT_CONTROL_STATE.value,
                        control_revision=0,
                        policy_version=INTERVENTION_POLICY_VERSION,
                        review_points=[],
                        last_event_seq=0,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
        except IntegrityError:
            pass
        return await self.get_control(episode_id, lock="update")

    async def update_control(
        self, episode_id: uuid.UUID | str, change: ControlChange
    ) -> EpisodeControl:
        """``lock_control`` 済みの行を更新する。既定で ``control_revision`` を1増やす。"""
        values: dict[str, Any] = {"updated_at": _now()}
        if change.mode is not None:
            values["mode"] = change.mode.value
        if change.control_state is not None:
            values["control_state"] = change.control_state.value
        if change.review_points is not None:
            values["review_points"] = [p.value for p in change.review_points]
        if change.active_manifest_id is not None:
            values["active_manifest_id"] = _uuid(change.active_manifest_id)
        if change.clear_waiting:
            values["waiting_point"] = None
            values["waiting_manifest_id"] = None
        else:
            if change.waiting_point is not None:
                values["waiting_point"] = change.waiting_point.value
            if change.waiting_manifest_id is not None:
                values["waiting_manifest_id"] = _uuid(change.waiting_manifest_id)
        if change.clear_hold_times:
            values["hold_requested_at"] = None
            values["held_at"] = None
        else:
            if change.hold_requested_at is not None:
                values["hold_requested_at"] = change.hold_requested_at
            if change.held_at is not None:
                values["held_at"] = change.held_at
        if change.bump_revision:
            values["control_revision"] = EpisodeControlRow.control_revision + 1
        await self._session.execute(
            update(EpisodeControlRow)
            .where(EpisodeControlRow.episode_id == _uuid(episode_id))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        row = await self._session.get(EpisodeControlRow, _uuid(episode_id), populate_existing=True)
        assert row is not None, "update_control requires lock_control first"
        return _to_control(row)

    # ---------------------------------------------------------------- イベント（正本）
    async def append_event(
        self,
        episode_id: uuid.UUID | str,
        event: InterventionEvent,
        actor: Actor,
        refs: Mapping[str, Any],
        *,
        command_id: str | None = None,
    ) -> EpisodeEvent:
        """Episode 内の次の ``seq`` でイベントを足す。制御行を lock してから呼ぶ。"""
        control = await self.lock_control(episode_id)
        seq = control.last_event_seq + 1
        await self._session.execute(
            update(EpisodeControlRow)
            .where(EpisodeControlRow.episode_id == _uuid(episode_id))
            .values(last_event_seq=seq)
            .execution_options(synchronize_session=False)
        )
        row = EpisodeEventRow(
            episode_id=_uuid(episode_id),
            seq=seq,
            event_name=event.value,
            actor_type=actor.type.value,
            actor_id=actor.id,
            refs=dict(refs),
            command_id=command_id,
            created_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        appended = _to_event(row)
        # ログへの投影は commit が成功した後にだけ出る（rollback では出ない。ADR-0041 §7 / INV-38）
        from infrastructure.intervention.projection import defer_event

        defer_event(self._session, appended)
        return appended

    async def list_events(
        self, episode_id: uuid.UUID | str, *, after_seq: int = 0
    ) -> list[EpisodeEvent]:
        rows = await self._session.scalars(
            select(EpisodeEventRow)
            .where(EpisodeEventRow.episode_id == _uuid(episode_id), EpisodeEventRow.seq > after_seq)
            .order_by(EpisodeEventRow.seq)
        )
        return [_to_event(r) for r in rows]

    async def list_unprojected_events(self, limit: int = 500) -> list[EpisodeEvent]:
        rows = await self._session.scalars(
            select(EpisodeEventRow)
            .where(EpisodeEventRow.projected_at.is_(None))
            .order_by(EpisodeEventRow.created_at, EpisodeEventRow.seq)
            .limit(limit)
        )
        return [_to_event(r) for r in rows]

    async def list_events_page(
        self,
        *,
        episode_id: uuid.UUID | str | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
        only_unprojected: bool = False,
        after: tuple[str, int] | None = None,
        limit: int = 500,
    ) -> list[EpisodeEvent]:
        """再投影の読み取り。``(episode_id, seq)`` 順の keyset で頁送りする。

        ``after`` を渡すとその行の次から読む。
        """
        stmt = select(EpisodeEventRow)
        if episode_id is not None:
            stmt = stmt.where(EpisodeEventRow.episode_id == _uuid(episode_id))
        if from_seq is not None:
            stmt = stmt.where(EpisodeEventRow.seq >= from_seq)
        if to_seq is not None:
            stmt = stmt.where(EpisodeEventRow.seq <= to_seq)
        if only_unprojected:
            stmt = stmt.where(EpisodeEventRow.projected_at.is_(None))
        if after is not None:
            after_episode, after_seq = _uuid(after[0]), after[1]
            same_episode_later = (EpisodeEventRow.episode_id == after_episode) & (
                EpisodeEventRow.seq > after_seq
            )
            stmt = stmt.where((EpisodeEventRow.episode_id > after_episode) | same_episode_later)
        rows = await self._session.scalars(
            stmt.order_by(EpisodeEventRow.episode_id, EpisodeEventRow.seq).limit(limit)
        )
        return [_to_event(r) for r in rows]

    async def mark_events_projected(self, episode_id: uuid.UUID | str, seqs: Sequence[int]) -> None:
        if not seqs:
            return
        await self._session.execute(
            update(EpisodeEventRow)
            .where(EpisodeEventRow.episode_id == _uuid(episode_id), EpisodeEventRow.seq.in_(seqs))
            .values(projected_at=_now())
            .execution_options(synchronize_session=False)
        )

    # ---------------------------------------------------------------- command
    async def insert_command(
        self,
        *,
        command_id: str,
        episode_id: uuid.UUID | str,
        type: CommandType,
        actor: Actor,
        granted_scope: Scope,
        expected_control_revision: int | None,
        expected_target_revision: str | None,
        payload: Mapping[str, Any],
        payload_sha256: str,
        correlation_id: str | None,
    ) -> tuple[Command, bool]:
        """command を RECEIVED で保存する。``(command, created)``。

        同じ id・同じ payload の再送は既存行を返す（created=False）。違う payload は
        ``DuplicateCommandError``。
        """
        existing = await self.get_command(command_id)
        if existing is not None:
            if existing.payload_sha256 != payload_sha256 or existing.episode_id != str(episode_id):
                raise DuplicateCommandError(existing)
            return existing, False
        row = InterventionCommandRow(
            id=command_id,
            episode_id=_uuid(episode_id),
            type=type.value,
            status=CommandStatus.RECEIVED.value,
            actor_type=actor.type.value,
            actor_id=actor.id,
            granted_scope=granted_scope.value,
            expected_control_revision=expected_control_revision,
            expected_target_revision=expected_target_revision,
            payload=dict(payload),
            payload_sha256=payload_sha256,
            correlation_id=correlation_id,
            schema_version=INTERVENTION_SCHEMA_VERSION,
            received_at=_now(),
            dispatch_attempts=0,
            updated_at=_now(),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError:
            # 並行した同じ command_id の INSERT。勝った方を同じ規則で判定する
            again = await self.get_command(command_id)
            if again is None:
                raise
            if again.payload_sha256 != payload_sha256:
                raise DuplicateCommandError(again) from None
            return again, False
        return _to_command(row), True

    async def get_command(self, command_id: str, *, lock: bool = False) -> Command | None:
        stmt = select(InterventionCommandRow).where(InterventionCommandRow.id == command_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            await self._session.refresh(row)
        return None if row is None else _to_command(row)

    async def set_command_status(
        self,
        command_id: str,
        status: CommandStatus,
        *,
        result: Mapping[str, Any] | None = None,
        reject_reason: RejectReason | None = None,
        executor: str | None = None,
    ) -> Command:
        values: dict[str, Any] = {"status": status.value, "updated_at": _now()}
        if result is not None:
            values["result"] = dict(result)
        if reject_reason is not None:
            values["reject_reason"] = reject_reason.value
        if executor is not None:
            values["executor"] = executor
        if status in {
            CommandStatus.APPLIED,
            CommandStatus.CONFLICT,
            CommandStatus.REJECTED,
            CommandStatus.FAILED,
        }:
            values["finished_at"] = _now()
        await self._session.execute(
            update(InterventionCommandRow)
            .where(InterventionCommandRow.id == command_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        command = await self.get_command(command_id)
        assert command is not None
        return command

    async def merge_command_result(self, command_id: str, extra: Mapping[str, Any]) -> Command:
        """終端後の追記（pipeline の start 結果など）。既存の result のキーは上書きしない。"""
        command = await self.get_command(command_id, lock=True)
        assert command is not None
        merged = dict(command.result or {})
        for key, value in extra.items():
            merged.setdefault(key, value)
        await self._session.execute(
            update(InterventionCommandRow)
            .where(InterventionCommandRow.id == command_id)
            .values(result=merged, updated_at=_now())
            .execution_options(synchronize_session=False)
        )
        refreshed = await self.get_command(command_id)
        assert refreshed is not None
        return refreshed

    async def mark_command_dispatched(self, command_id: str) -> None:
        await self._session.execute(
            update(InterventionCommandRow)
            .where(InterventionCommandRow.id == command_id)
            .values(
                dispatched_at=_now(),
                dispatch_attempts=InterventionCommandRow.dispatch_attempts + 1,
                updated_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )

    async def list_undelivered_commands(
        self, *, stale_before: datetime, limit: int = 100
    ) -> list[Command]:
        """未配送、または配送したが終端にならず ``stale_before`` より前に受けた command。"""
        open_statuses = [
            CommandStatus.RECEIVED.value,
            CommandStatus.VALIDATED.value,
            CommandStatus.APPLYING.value,
        ]
        rows = await self._session.scalars(
            select(InterventionCommandRow)
            .where(
                InterventionCommandRow.status.in_(open_statuses),
                (InterventionCommandRow.dispatched_at.is_(None))
                | (InterventionCommandRow.received_at < stale_before),
            )
            .order_by(InterventionCommandRow.received_at)
            .limit(limit)
        )
        return [_to_command(r) for r in rows]

    async def list_commands(self, episode_id: uuid.UUID | str) -> list[Command]:
        rows = await self._session.scalars(
            select(InterventionCommandRow)
            .where(InterventionCommandRow.episode_id == _uuid(episode_id))
            .order_by(InterventionCommandRow.received_at)
        )
        return [_to_command(r) for r in rows]

    # ---------------------------------------------------------------- manifest
    async def insert_manifest(
        self,
        episode_id: uuid.UUID | str,
        members: Mapping[str, str],
        *,
        actor: Actor,
        parent_manifest_id: str | None,
        causation_command_id: str | None = None,
    ) -> Manifest:
        """次の ``seq`` の manifest を足す。制御行を lock してから呼ぶ（seq の採番を直列化）。"""
        await self.lock_control(episode_id)
        latest = await self._session.scalar(
            select(func.max(ArtifactManifestRow.seq)).where(
                ArtifactManifestRow.episode_id == _uuid(episode_id)
            )
        )
        row = ArtifactManifestRow(
            episode_id=_uuid(episode_id),
            seq=(latest or 0) + 1,
            parent_manifest_id=_opt_uuid(parent_manifest_id),
            members={str(k): str(v) for k, v in members.items()},
            causation_command_id=causation_command_id,
            actor_type=actor.type.value,
            actor_id=actor.id,
            schema_version=INTERVENTION_SCHEMA_VERSION,
            created_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_manifest(row)

    async def get_manifest(self, manifest_id: uuid.UUID | str) -> Manifest | None:
        row = await self._session.get(ArtifactManifestRow, _uuid(manifest_id))
        return None if row is None else _to_manifest(row)

    async def list_manifests(self, episode_id: uuid.UUID | str) -> list[Manifest]:
        rows = await self._session.scalars(
            select(ArtifactManifestRow)
            .where(ArtifactManifestRow.episode_id == _uuid(episode_id))
            .order_by(ArtifactManifestRow.seq)
        )
        return [_to_manifest(r) for r in rows]

    # ---------------------------------------------------------------- revision links
    async def add_link(
        self,
        episode_id: uuid.UUID | str,
        upstream_revision_id: uuid.UUID | str,
        downstream_revision_id: uuid.UUID | str,
        kind: RevisionLinkKind,
        *,
        causation_command_id: str | None = None,
    ) -> None:
        """同じ辺の再記録は no-op（冪等）。"""
        exists = await self._session.scalar(
            select(RevisionLinkRow.id).where(
                RevisionLinkRow.upstream_revision_id == _uuid(upstream_revision_id),
                RevisionLinkRow.downstream_revision_id == _uuid(downstream_revision_id),
                RevisionLinkRow.kind == kind.value,
            )
        )
        if exists is not None:
            return
        try:
            async with self._session.begin_nested():
                self._session.add(
                    RevisionLinkRow(
                        episode_id=_uuid(episode_id),
                        upstream_revision_id=_uuid(upstream_revision_id),
                        downstream_revision_id=_uuid(downstream_revision_id),
                        kind=kind.value,
                        causation_command_id=causation_command_id,
                        created_at=_now(),
                    )
                )
        except IntegrityError:
            pass

    async def links(
        self,
        *,
        episode_id: uuid.UUID | str | None = None,
        downstream_revision_id: uuid.UUID | str | None = None,
        upstream_revision_id: uuid.UUID | str | None = None,
        kind: RevisionLinkKind | None = None,
    ) -> list[RevisionLink]:
        stmt = select(RevisionLinkRow)
        if episode_id is not None:
            stmt = stmt.where(RevisionLinkRow.episode_id == _uuid(episode_id))
        if downstream_revision_id is not None:
            stmt = stmt.where(
                RevisionLinkRow.downstream_revision_id == _uuid(downstream_revision_id)
            )
        if upstream_revision_id is not None:
            stmt = stmt.where(RevisionLinkRow.upstream_revision_id == _uuid(upstream_revision_id))
        if kind is not None:
            stmt = stmt.where(RevisionLinkRow.kind == kind.value)
        rows = await self._session.scalars(stmt.order_by(RevisionLinkRow.created_at))
        return [
            RevisionLink(
                upstream_revision_id=str(r.upstream_revision_id),
                downstream_revision_id=str(r.downstream_revision_id),
                kind=RevisionLinkKind(r.kind),
                causation_command_id=r.causation_command_id,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # ---------------------------------------------------------------- 判断記録・実行記録
    async def insert_agent_run(
        self,
        *,
        idempotency_key: str,
        role: AgentRole,
        step: str,
        snapshot_object_key: str,
        snapshot_sha256: str,
        outcome: AgentRunOutcome,
        episode_id: uuid.UUID | str | None = None,
        topic_plan_id: uuid.UUID | str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        request_object_key: str | None = None,
        request_sha256: str | None = None,
        response_object_key: str | None = None,
        response_sha256: str | None = None,
        tool_invocations: Sequence[Mapping[str, Any]] = (),
        token_usage: Mapping[str, Any] | None = None,
        estimated_cost_usd: Decimal | None = None,
        output_revision_id: uuid.UUID | str | None = None,
        error_summary: str | None = None,
        correlation_id: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        agent_run_id: uuid.UUID | str | None = None,
    ) -> AgentRun:
        """同じ ``idempotency_key`` の再記録は既存行を返す（Activity の再実行）。

        ``agent_run_id`` を渡すとその id で作る（snapshot の保存キーに id を先に使うため）。
        """
        existing = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return _to_agent_run(existing)
        row = AgentRunRow(
            id=_uuid(agent_run_id) if agent_run_id is not None else uuid.uuid4(),
            idempotency_key=idempotency_key,
            episode_id=_opt_uuid(episode_id),
            topic_plan_id=_opt_uuid(topic_plan_id),
            role=role.value,
            step=step,
            model=model,
            prompt_version=prompt_version,
            snapshot_object_key=snapshot_object_key,
            snapshot_sha256=snapshot_sha256,
            request_object_key=request_object_key,
            request_sha256=request_sha256,
            response_object_key=response_object_key,
            response_sha256=response_sha256,
            tool_invocations=[dict(t) for t in tool_invocations],
            token_usage=None if token_usage is None else dict(token_usage),
            estimated_cost_usd=estimated_cost_usd,
            output_revision_id=_opt_uuid(output_revision_id),
            outcome=outcome.value,
            error_summary=None if error_summary is None else error_summary[:1000],
            correlation_id=correlation_id,
            schema_version=INTERVENTION_SCHEMA_VERSION,
            started_at=started_at,
            finished_at=finished_at,
            created_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_agent_run(row)

    async def get_agent_run(self, agent_run_id: uuid.UUID | str) -> AgentRun | None:
        row = await self._session.get(AgentRunRow, _uuid(agent_run_id))
        return None if row is None else _to_agent_run(row)

    async def list_agent_runs(
        self,
        *,
        episode_id: uuid.UUID | str | None = None,
        topic_plan_id: uuid.UUID | str | None = None,
        output_revision_id: uuid.UUID | str | None = None,
    ) -> list[AgentRun]:
        stmt = select(AgentRunRow)
        if episode_id is not None:
            stmt = stmt.where(AgentRunRow.episode_id == _uuid(episode_id))
        if topic_plan_id is not None:
            stmt = stmt.where(AgentRunRow.topic_plan_id == _uuid(topic_plan_id))
        if output_revision_id is not None:
            stmt = stmt.where(AgentRunRow.output_revision_id == _uuid(output_revision_id))
        rows = await self._session.scalars(stmt.order_by(AgentRunRow.created_at))
        return [_to_agent_run(r) for r in rows]

    async def insert_decision(
        self,
        *,
        idempotency_key: str,
        decision_type: str,
        actor: Actor,
        payload: Mapping[str, Any],
        episode_id: uuid.UUID | str | None = None,
        topic_plan_id: uuid.UUID | str | None = None,
        agent_run_id: uuid.UUID | str | None = None,
        result_revision_id: uuid.UUID | str | None = None,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DecisionRecord:
        existing = await self._session.scalar(
            select(DecisionRecordRow).where(DecisionRecordRow.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return _to_decision(existing)
        row = DecisionRecordRow(
            idempotency_key=idempotency_key,
            episode_id=_opt_uuid(episode_id),
            topic_plan_id=_opt_uuid(topic_plan_id),
            agent_run_id=_opt_uuid(agent_run_id),
            decision_type=decision_type,
            actor_type=actor.type.value,
            actor_id=actor.id,
            payload=dict(payload),
            result_revision_id=_opt_uuid(result_revision_id),
            command_id=command_id,
            correlation_id=correlation_id,
            schema_version=INTERVENTION_SCHEMA_VERSION,
            created_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_decision(row)

    async def list_decisions(
        self,
        *,
        episode_id: uuid.UUID | str | None = None,
        topic_plan_id: uuid.UUID | str | None = None,
        result_revision_id: uuid.UUID | str | None = None,
    ) -> list[DecisionRecord]:
        stmt = select(DecisionRecordRow)
        if episode_id is not None:
            stmt = stmt.where(DecisionRecordRow.episode_id == _uuid(episode_id))
        if topic_plan_id is not None:
            stmt = stmt.where(DecisionRecordRow.topic_plan_id == _uuid(topic_plan_id))
        if result_revision_id is not None:
            stmt = stmt.where(DecisionRecordRow.result_revision_id == _uuid(result_revision_id))
        rows = await self._session.scalars(stmt.order_by(DecisionRecordRow.created_at))
        return [_to_decision(r) for r in rows]

    # ---------------------------------------------------------------- 注記
    async def insert_annotation(
        self,
        *,
        episode_id: uuid.UUID | str,
        kind: AnnotationKind,
        anchor: Mapping[str, Any],
        body: str,
        actor: Actor,
        payload: Mapping[str, Any] | None = None,
        refers_to_annotation_id: uuid.UUID | str | None = None,
        manifest_id: uuid.UUID | str | None = None,
        command_id: str | None = None,
        agent_run_id: uuid.UUID | str | None = None,
    ) -> Annotation:
        """``command_id`` 付きの再記録は既存行を返す（command の再適用で注記を増やさない）。"""
        if command_id is not None:
            existing = await self._session.scalar(
                select(AnnotationRow).where(AnnotationRow.command_id == command_id)
            )
            if existing is not None:
                return _to_annotation(existing)
        row = AnnotationRow(
            episode_id=_uuid(episode_id),
            kind=kind.value,
            anchor=dict(anchor),
            body=body,
            payload=None if payload is None else dict(payload),
            refers_to_annotation_id=_opt_uuid(refers_to_annotation_id),
            manifest_id=_opt_uuid(manifest_id),
            actor_type=actor.type.value,
            actor_id=actor.id,
            command_id=command_id,
            agent_run_id=_opt_uuid(agent_run_id),
            schema_version=INTERVENTION_SCHEMA_VERSION,
            created_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_annotation(row)

    async def list_annotations(
        self, episode_id: uuid.UUID | str, *, kind: AnnotationKind | None = None
    ) -> list[Annotation]:
        stmt = select(AnnotationRow).where(AnnotationRow.episode_id == _uuid(episode_id))
        if kind is not None:
            stmt = stmt.where(AnnotationRow.kind == kind.value)
        rows = await self._session.scalars(stmt.order_by(AnnotationRow.created_at))
        return [_to_annotation(r) for r in rows]

    # ---------------------------------------------------------------- change set
    async def insert_change_set(
        self,
        *,
        episode_id: uuid.UUID | str,
        kind: ChangeKind,
        base_revision_id: uuid.UUID | str,
        proposal: Mapping[str, Any],
        actor: Actor,
        proposed_by_command_id: str,
        base_manifest_id: uuid.UUID | str | None = None,
        impact: Mapping[str, Any] | None = None,
        estimated_cost_usd: Decimal | None = None,
        rationale: str | None = None,
        variant_group: str | None = None,
    ) -> ChangeSet:
        existing = await self._session.scalar(
            select(ChangeSetRow).where(
                ChangeSetRow.proposed_by_command_id == proposed_by_command_id
            )
        )
        if existing is not None:
            return _to_change_set(existing)
        row = ChangeSetRow(
            episode_id=_uuid(episode_id),
            kind=kind.value,
            status=ChangeSetStatus.PROPOSED.value,
            base_revision_id=_uuid(base_revision_id),
            base_manifest_id=_opt_uuid(base_manifest_id),
            proposal=dict(proposal),
            impact=None if impact is None else dict(impact),
            estimated_cost_usd=estimated_cost_usd,
            rationale=rationale,
            variant_group=variant_group,
            proposed_by_command_id=proposed_by_command_id,
            actor_type=actor.type.value,
            actor_id=actor.id,
            schema_version=INTERVENTION_SCHEMA_VERSION,
            created_at=_now(),
            updated_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_change_set(row)

    async def get_change_set(
        self, change_set_id: uuid.UUID | str, *, lock: bool = False
    ) -> ChangeSet | None:
        stmt = select(ChangeSetRow).where(ChangeSetRow.id == _uuid(change_set_id))
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            await self._session.refresh(row)
        return None if row is None else _to_change_set(row)

    async def set_change_set_status(
        self,
        change_set_id: uuid.UUID | str,
        status: ChangeSetStatus,
        *,
        decided_by_command_id: str | None = None,
        applied_revision_id: uuid.UUID | str | None = None,
        applied_manifest_id: uuid.UUID | str | None = None,
        impact: Mapping[str, Any] | None = None,
    ) -> ChangeSet:
        values: dict[str, Any] = {"status": status.value, "updated_at": _now()}
        if decided_by_command_id is not None:
            values["decided_by_command_id"] = decided_by_command_id
        if applied_revision_id is not None:
            values["applied_revision_id"] = _uuid(applied_revision_id)
        if applied_manifest_id is not None:
            values["applied_manifest_id"] = _uuid(applied_manifest_id)
        if impact is not None:
            values["impact"] = dict(impact)
        await self._session.execute(
            update(ChangeSetRow)
            .where(ChangeSetRow.id == _uuid(change_set_id))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        change_set = await self.get_change_set(change_set_id)
        assert change_set is not None
        return change_set

    async def list_change_sets(self, episode_id: uuid.UUID | str) -> list[ChangeSet]:
        rows = await self._session.scalars(
            select(ChangeSetRow)
            .where(ChangeSetRow.episode_id == _uuid(episode_id))
            .order_by(ChangeSetRow.created_at)
        )
        return [_to_change_set(r) for r in rows]
