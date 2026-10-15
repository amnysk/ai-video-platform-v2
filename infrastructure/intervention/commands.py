"""介入 command の受付と適用（``docs/domain/intervention.md`` §2・§4・§5、ADR-0041）。

- ``receive_command``: API が**1 transaction**で呼ぶ（commit は呼び出し側）。型・scope・payload・
  Episode の存在を検査し、command 行（transactional outbox）を ``received`` で保存する。
  同じ id・同じ payload の再送は既存行を返し、違う payload は ``DuplicateCommandError``（409）。
  ``request_hold`` はここで ``hold_requested`` にする（dispatch gate は commit の時点から
  新しい送信を拒否）。副作用の無い ``comment`` / ``set_user_constraint`` と、記録だけの
  ``request_cancel`` はここで適用する。
- ``apply_command``: command workflow の Activity の本体。Episode の制御行を lock して直列化し、
  終端済みなら**保存済みの result をそのまま返す**（再配送・復旧の冪等 / INV-42）。
  検証（scope・期待版・予算・制約）→ ``applied`` / ``conflict`` / ``rejected`` / ``failed``。
  状態・新 revision・manifest・判断記録・イベントは**同じ commit**に入る。

``received`` を「修正完了」として返さない。外部生成を伴う変更の ``applied`` と
完成動画の生成完了も別。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.intervention import (
    CO_PRODUCTION_REVIEW_POINTS,
    HUMAN_ONLY_SCOPES,
    PAYLOAD_MODELS,
    REQUIRED_SCOPE,
    TERMINAL_COMMAND_STATUSES,
    ActorType,
    AnnotationKind,
    CommandEnvelope,
    CommandStatus,
    CommandType,
    CommentPayload,
    ControlState,
    InterventionEvent,
    InterventionMode,
    RejectReason,
    ReleaseCheckpointPayload,
    ResolvePayload,
    ResumePayload,
    Scope,
    SetModePayload,
    UserConstraintPayload,
    payload_sha256,
)
from contracts.pipeline import STAGE_REVIEW_POINT, PipelineStage
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from domain.errors import TransientError
from domain.pipeline.resume_plan import determine_target_stage
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.gate import REVIEWING_MODES
from infrastructure.intervention.manifests import current_members
from infrastructure.intervention.store import (
    Actor,
    Command,
    ControlChange,
    DuplicateCommandError,
    EpisodeControl,
    InterventionStore,
    manifest_slot,
)
from infrastructure.storage.artifact_store import ArtifactStore

__all__ = [
    "ApplyContext",
    "CommandForbiddenError",
    "CommandNotFoundError",
    "CommandOutcome",
    "CommandReceiveError",
    "Decision",
    "DuplicateCommandError",
    "EpisodeNotFoundError",
    "InvalidCommandPayloadError",
    "Principal",
    "ReceiveResult",
    "UnsupportedCommandError",
    "apply_command",
    "receive_command",
    "reservation_listing",
]

#: 受付の transaction で適用まで済ませる型（副作用が無い・記録だけ）
INLINE_TYPES: frozenset[CommandType] = frozenset(
    {
        CommandType.COMMENT,
        CommandType.SET_USER_CONSTRAINT,
        CommandType.RESOLVE_ANNOTATION,
        CommandType.REQUEST_CANCEL,
    }
)
#: 期待版（expected_control_revision）を受付で検査し、適用では検査しない型。
#: 受付が制御状態を変える（制御版が進む）ので、適用時の比較は意味を持たない
_CHECKED_AT_RECEIVE: frozenset[CommandType] = frozenset({CommandType.REQUEST_HOLD})

#: 介入で変更を受け付けない Episode 状態（投稿済み・終端）。既存の投稿を変えない（AGENTS.md §9）
CLOSED_EPISODE_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {
        EpisodeStatus.UPLOADED,
        EpisodeStatus.ANALYZED,
        EpisodeStatus.COMPLETED,
        EpisodeStatus.FAILED,
        EpisodeStatus.CANCELLED,
    }
)

#: 工程の出力（resume で「どこから走らせるか」を現行の欠けから決める）
STAGE_OUTPUT: tuple[tuple[PipelineStage, ArtifactType], ...] = (
    (PipelineStage.SCRIPT, ArtifactType.SCRIPT),
    (PipelineStage.STORYBOARD, ArtifactType.STORYBOARD),
    (PipelineStage.PRODUCTION, ArtifactType.PRODUCTION_MANIFEST),
    (PipelineStage.RENDER, ArtifactType.FINAL_VIDEO),
    (PipelineStage.UPLOAD, ArtifactType.UPLOAD_RECEIPT),
)
_STAGES: tuple[PipelineStage, ...] = tuple(PipelineStage)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- 型


@dataclass(frozen=True, slots=True)
class Principal:
    """認証済みの主体（API の principal 設定から作る。担当 C）。"""

    actor: Actor
    scopes: frozenset[Scope]


class CommandReceiveError(Exception):
    """受付で拒否した（行を作らない）。``code`` は API の機械可読な理由。"""

    code: str = "invalid"


class UnsupportedCommandError(CommandReceiveError):
    code = RejectReason.UNSUPPORTED_COMMAND.value


class InvalidCommandPayloadError(CommandReceiveError):
    code = RejectReason.INVALID_PAYLOAD.value


class CommandForbiddenError(CommandReceiveError):
    code = RejectReason.FORBIDDEN.value


class EpisodeNotFoundError(CommandReceiveError):
    code = RejectReason.NOT_FOUND.value


class CommandNotFoundError(LookupError):
    """apply する command 行が無い（配送の誤り。再試行しても直らない）。"""


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    command: Command
    #: False = 同じ command_id・同じ payload の再送（既存行を返した）
    created: bool

    @property
    def needs_delivery(self) -> bool:
        """command workflow を start すべきか（終端なら配送しても保存済み result が返るだけ）。"""
        return self.command.status not in TERMINAL_COMMAND_STATUSES


@dataclass(frozen=True, slots=True)
class Decision:
    status: CommandStatus
    result: dict[str, Any]
    reason: RejectReason | None = None
    #: 適用後に command workflow が子 pipeline を start するときの入力
    pipeline_start: dict[str, Any] | None = None

    @classmethod
    def applied(
        cls,
        result: Mapping[str, Any] | None = None,
        *,
        pipeline_start: dict[str, Any] | None = None,
    ) -> Decision:
        return cls(CommandStatus.APPLIED, dict(result or {}), None, pipeline_start)

    @classmethod
    def rejected(cls, reason: RejectReason, **detail: Any) -> Decision:
        return cls(CommandStatus.REJECTED, {"reason": reason.value, **detail}, reason)

    @classmethod
    def conflict(cls, reason: RejectReason, **detail: Any) -> Decision:
        return cls(CommandStatus.CONFLICT, {"reason": reason.value, **detail}, reason)


@dataclass(slots=True)
class ApplyContext:
    session: AsyncSession
    store: InterventionStore
    artifact_store: ArtifactStore | None
    command: Command
    control: EpisodeControl
    budget_policy: BudgetPolicy
    now: datetime
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def episode_id(self) -> str:
        return self.command.episode_id

    @property
    def actor(self) -> Actor:
        return self.command.actor


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    command_id: str
    episode_id: str
    type: CommandType
    status: CommandStatus
    reject_reason: RejectReason | None
    result: dict[str, Any]
    #: True = 既に終端だった command の保存済み result を返した（再適用していない）
    replayed: bool

    @property
    def pipeline_start(self) -> dict[str, Any] | None:
        """子 pipeline の start がまだ記録されていなければ、その入力。"""
        if self.status is not CommandStatus.APPLIED or "pipeline" in self.result:
            return None
        start = self.result.get("pipeline_start")
        return dict(start) if isinstance(start, dict) else None

    @classmethod
    def of(cls, command: Command, *, replayed: bool) -> CommandOutcome:
        return cls(
            command_id=command.command_id,
            episode_id=command.episode_id,
            type=command.type,
            status=command.status,
            reject_reason=command.reject_reason,
            result=dict(command.result or {}),
            replayed=replayed,
        )


Handler = Callable[[ApplyContext], Awaitable[Decision]]

# --------------------------------------------------------------------------- 受付


async def receive_command(
    session: AsyncSession,
    episode_id: uuid.UUID | str,
    envelope: CommandEnvelope,
    principal: Principal,
    *,
    now: datetime | None = None,
) -> ReceiveResult:
    """command を受け付ける。commit は呼び出し側（API の1 transaction）。

    拒否（行を作らない）: ``UnsupportedCommandError`` / ``CommandForbiddenError`` /
    ``InvalidCommandPayloadError`` / ``EpisodeNotFoundError`` / ``DuplicateCommandError``。
    """
    now = now or _utcnow()
    scope = REQUIRED_SCOPE.get(envelope.type)
    if scope is None:
        raise UnsupportedCommandError(f"command type {envelope.type!r} is not supported")
    if scope not in principal.scopes:
        raise CommandForbiddenError(f"principal lacks scope {scope.value!r}")
    if scope in HUMAN_ONLY_SCOPES and principal.actor.type is not ActorType.HUMAN:
        raise CommandForbiddenError(f"scope {scope.value!r} is for human principals only")
    try:
        PAYLOAD_MODELS[envelope.type].model_validate(envelope.payload)
    except ValidationError as exc:
        raise InvalidCommandPayloadError(_validation_summary(exc)) from None
    if await EpisodeRepository(session).get(episode_id) is None:
        raise EpisodeNotFoundError(f"episode {episode_id} not found")

    ep = str(episode_id)
    store = InterventionStore(session)
    control = await store.lock_control(ep)
    command, created = await store.insert_command(
        command_id=envelope.command_id,
        episode_id=ep,
        type=envelope.type,
        actor=principal.actor,
        granted_scope=scope,
        expected_control_revision=envelope.expected_control_revision,
        expected_target_revision=envelope.expected_target_revision,
        payload=envelope.payload,
        payload_sha256=payload_sha256(envelope),
        correlation_id=envelope.correlation_id,
    )
    if not created:
        return ReceiveResult(command, created=False)
    await store.append_event(
        ep,
        InterventionEvent.COMMAND_RECEIVED,
        principal.actor,
        {"type": envelope.type.value, "granted_scope": scope.value},
        command_id=command.command_id,
    )
    if envelope.type is CommandType.REQUEST_HOLD:
        await _accept_hold(store, command, control, now)
    elif envelope.type in INLINE_TYPES:
        ctx = ApplyContext(session, store, None, command, control, BudgetPolicy(), now)
        decision = await _decide(ctx)
        await _finish(ctx, decision, executor="api")
    refreshed = await store.get_command(command.command_id)
    assert refreshed is not None
    return ReceiveResult(refreshed, created=True)


async def _accept_hold(
    store: InterventionStore, command: Command, control: EpisodeControl, now: datetime
) -> None:
    """``request_hold`` の受付: 同じ transaction で ``hold_requested`` にする（§2）。"""
    expected = command.expected_control_revision
    if expected is not None and expected != control.control_revision:
        ctx = ApplyContext(store.session, store, None, command, control, BudgetPolicy(), now)
        await _finish(
            ctx,
            Decision.conflict(
                RejectReason.STALE_CONTROL_REVISION,
                expected=expected,
                current=control.control_revision,
            ),
            executor="api",
        )
        return
    if control.control_state in (ControlState.RUNNING, ControlState.WAITING_REVIEW):
        await store.update_control(
            command.episode_id,
            ControlChange(control_state=ControlState.HOLD_REQUESTED, hold_requested_at=now),
        )
        await store.append_event(
            command.episode_id,
            InterventionEvent.HOLD_REQUESTED,
            command.actor,
            {"previous_state": control.control_state.value},
            command_id=command.command_id,
        )


def _validation_summary(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
    return "; ".join(parts)[:1000]


# --------------------------------------------------------------------------- 適用


async def apply_command(
    session_factory: async_sessionmaker[AsyncSession],
    artifact_store: ArtifactStore,
    command_id: str,
    *,
    budget_policy: BudgetPolicy,
    clock: Callable[[], datetime] = _utcnow,
    executor: str = "intervention-worker",
) -> CommandOutcome:
    """command を適用する（冪等）。終端済みなら保存済み result を返す。

    一時的な障害（DB 接続・ストアの通信）は例外のまま上げる（Activity の retry）。
    それ以外の予期しない例外は、変更を全て捨てたうえで ``failed``（``internal_error``）を記録する。
    """
    try:
        async with session_factory() as session:
            outcome = await _apply_in_session(
                session, artifact_store, command_id, budget_policy, clock(), executor
            )
            await session.commit()
            return outcome
    except CommandNotFoundError:
        raise
    except Exception as exc:
        if _is_transient(exc):
            raise
        return await _record_failure(session_factory, command_id, exc, executor)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, OperationalError | InterfaceError | TransientError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, ConnectionError | TimeoutError)


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    command_id: str,
    exc: BaseException,
    executor: str,
) -> CommandOutcome:
    async with session_factory() as session:
        store = InterventionStore(session)
        command = await store.get_command(command_id)
        if command is None:
            raise CommandNotFoundError(command_id) from exc
        control = await store.lock_control(command.episode_id)
        command = await store.get_command(command_id, lock=True)
        assert command is not None
        if command.status in TERMINAL_COMMAND_STATUSES:
            await session.commit()
            return CommandOutcome.of(command, replayed=True)
        ctx = ApplyContext(session, store, None, command, control, BudgetPolicy(), _utcnow())
        # 例外文は写さない（秘密・URL が混ざりうる / INV-20）。型だけを残す
        decision = Decision(
            CommandStatus.FAILED,
            {"reason": RejectReason.INTERNAL_ERROR.value, "error_type": type(exc).__name__},
            RejectReason.INTERNAL_ERROR,
        )
        finished = await _finish(ctx, decision, executor=executor)
        await session.commit()
        return CommandOutcome.of(finished, replayed=False)


async def _apply_in_session(
    session: AsyncSession,
    artifact_store: ArtifactStore,
    command_id: str,
    budget_policy: BudgetPolicy,
    now: datetime,
    executor: str,
) -> CommandOutcome:
    store = InterventionStore(session)
    command = await store.get_command(command_id)
    if command is None:
        raise CommandNotFoundError(command_id)
    # Episode 内の適用を直列化する（受付の hold・dispatch gate と同じ行）
    control = await store.lock_control(command.episode_id)
    command = await store.get_command(command_id, lock=True)
    assert command is not None
    if command.status in TERMINAL_COMMAND_STATUSES:
        return CommandOutcome.of(command, replayed=True)
    ctx = ApplyContext(session, store, artifact_store, command, control, budget_policy, now)
    decision = await _decide(ctx)
    finished = await _finish(ctx, decision, executor=executor)
    return CommandOutcome.of(finished, replayed=False)


async def _decide(ctx: ApplyContext) -> Decision:
    command = ctx.command
    if command.granted_scope is not REQUIRED_SCOPE[command.type]:
        return Decision.rejected(RejectReason.FORBIDDEN, detail="granted scope does not match")
    if (
        REQUIRED_SCOPE[command.type] in HUMAN_ONLY_SCOPES
        and command.actor.type is not ActorType.HUMAN
    ):
        return Decision.rejected(RejectReason.FORBIDDEN, detail="human principals only")
    expected = command.expected_control_revision
    if (
        command.type not in _CHECKED_AT_RECEIVE
        and expected is not None
        and expected != ctx.control.control_revision
    ):
        # INV-43: 上書きしない。差分は再取得させる
        return Decision.conflict(
            RejectReason.STALE_CONTROL_REVISION,
            expected=expected,
            current=ctx.control.control_revision,
        )
    try:
        PAYLOAD_MODELS[command.type].model_validate(command.payload)
    except ValidationError as exc:
        return Decision.rejected(RejectReason.INVALID_PAYLOAD, detail=_validation_summary(exc))
    handler = _handlers().get(command.type)
    if handler is None:
        return Decision.rejected(RejectReason.UNSUPPORTED_COMMAND)
    return await handler(ctx)


async def _finish(ctx: ApplyContext, decision: Decision, *, executor: str) -> Command:
    store = ctx.store
    control = await store.get_control(ctx.episode_id, lock="update")
    result = dict(decision.result)
    result["control_revision"] = control.control_revision
    result["control_state"] = control.control_state.value
    if decision.pipeline_start is not None:
        result["pipeline_start"] = decision.pipeline_start
    finished = await store.set_command_status(
        ctx.command.command_id,
        decision.status,
        result=result,
        reject_reason=decision.reason,
        executor=executor,
    )
    await store.append_event(
        ctx.episode_id,
        InterventionEvent.COMMAND_FINISHED,
        ctx.actor,
        {
            "type": ctx.command.type.value,
            "status": decision.status.value,
            "reject_reason": None if decision.reason is None else decision.reason.value,
        },
        command_id=ctx.command.command_id,
    )
    return finished


async def record_followup(
    session_factory: async_sessionmaker[AsyncSession],
    command_id: str,
    pipeline: Mapping[str, Any],
) -> CommandOutcome:
    """子 pipeline の start の結果（started / already_running）を command の result に追記する。"""
    async with session_factory() as session:
        store = InterventionStore(session)
        command = await store.get_command(command_id)
        if command is None:
            raise CommandNotFoundError(command_id)
        await store.lock_control(command.episode_id)
        merged = await store.merge_command_result(command_id, {"pipeline": dict(pipeline)})
        if merged.type is CommandType.RESUME and pipeline.get("outcome") in {
            "started",
            "already_running",
        }:
            await store.append_event(
                merged.episode_id,
                InterventionEvent.RESUME_COMPLETED,
                merged.actor,
                {"pipeline": dict(pipeline)},
                command_id=command_id,
            )
        await session.commit()
        return CommandOutcome.of(merged, replayed=False)


# --------------------------------------------------------------------------- 型ごとの適用


def _handlers() -> dict[CommandType, Handler]:
    # ChangeSet の適用は provider の見積り式を読む。API（受付）が provider のモジュールを
    # 読み込まないよう、適用側でだけ import する
    from infrastructure.intervention import changes

    return {
        CommandType.COMMENT: _comment,
        CommandType.SET_USER_CONSTRAINT: _user_constraint,
        CommandType.RESOLVE_ANNOTATION: _resolve_annotation,
        CommandType.REQUEST_HOLD: _hold,
        CommandType.REQUEST_CANCEL: _cancel,
        CommandType.RESUME: _resume,
        CommandType.RELEASE_CHECKPOINT: _release_checkpoint,
        CommandType.SET_MODE: _set_mode,
        CommandType.PROPOSE_CHANGE: changes.propose_change,
        CommandType.APPLY_CHANGE: changes.apply_change,
        CommandType.REJECT_CHANGE: changes.reject_change,
    }


async def _revision_of_episode(ctx: ApplyContext, revision_id: str) -> bool:
    from infrastructure.db.repositories import ArtifactMetadataRepository

    try:
        meta = await ArtifactMetadataRepository(ctx.session).get(revision_id)
    except ValueError:
        return False
    return meta is not None and meta.episode_id == ctx.episode_id


async def _comment(ctx: ApplyContext) -> Decision:
    payload = CommentPayload.model_validate(ctx.command.payload)
    anchor = payload.anchor
    if anchor.revision_id is not None and not await _revision_of_episode(ctx, anchor.revision_id):
        return Decision.rejected(RejectReason.NOT_FOUND, detail="anchor revision not found")
    if anchor.manifest_id is not None:
        manifest = await ctx.store.get_manifest(anchor.manifest_id)
        if manifest is None or manifest.episode_id != ctx.episode_id:
            return Decision.rejected(RejectReason.NOT_FOUND, detail="anchor manifest not found")
    annotation = await ctx.store.insert_annotation(
        episode_id=ctx.episode_id,
        kind=AnnotationKind(payload.kind),
        anchor=anchor.model_dump(mode="json", exclude_none=True),
        body=payload.body,
        actor=ctx.actor,
        manifest_id=anchor.manifest_id,
        command_id=ctx.command.command_id,
    )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.COMMENT_ADDED,
        ctx.actor,
        {"annotation_id": annotation.annotation_id, "kind": annotation.kind.value},
        command_id=ctx.command.command_id,
    )
    return Decision.applied({"annotation_id": annotation.annotation_id})


async def _resolve_annotation(ctx: ApplyContext) -> Decision:
    """resolution 注記を追記し review.resolved を記録する（元の注記は変えない / INV-41）。"""
    payload = ResolvePayload.model_validate(ctx.command.payload)
    annotations = await ctx.store.list_annotations(ctx.episode_id)
    target = next((a for a in annotations if a.annotation_id == payload.annotation_id), None)
    if target is None:
        return Decision.rejected(RejectReason.NOT_FOUND, detail="annotation not found")
    if target.kind not in (AnnotationKind.COMMENT, AnnotationKind.REVIEW_FINDING):
        return Decision.rejected(
            RejectReason.INVALID_PAYLOAD, detail="only comments and review findings are resolved"
        )
    if any(
        a.kind is AnnotationKind.RESOLUTION and a.refers_to_annotation_id == target.annotation_id
        for a in annotations
    ):
        return Decision.rejected(RejectReason.INVALID_STATE, detail="already resolved")
    resolution = await ctx.store.insert_annotation(
        episode_id=ctx.episode_id,
        kind=AnnotationKind.RESOLUTION,
        anchor={},
        body=payload.note or payload.resolution.value,
        actor=ctx.actor,
        payload={"resolution": payload.resolution.value},
        refers_to_annotation_id=target.annotation_id,
        command_id=ctx.command.command_id,
    )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.REVIEW_RESOLVED,
        ctx.actor,
        {
            "annotation_id": target.annotation_id,
            "resolution_id": resolution.annotation_id,
            "resolution": payload.resolution.value,
        },
        command_id=ctx.command.command_id,
    )
    return Decision.applied(
        {"annotation_id": target.annotation_id, "resolution_id": resolution.annotation_id}
    )


async def _user_constraint(ctx: ApplyContext) -> Decision:
    payload = UserConstraintPayload.model_validate(ctx.command.payload)
    annotation = await ctx.store.insert_annotation(
        episode_id=ctx.episode_id,
        kind=AnnotationKind.USER_CONSTRAINT,
        anchor={},
        body=payload.note or payload.rule,
        actor=ctx.actor,
        payload={"rule": payload.rule, "target_ids": list(payload.target_ids)},
        command_id=ctx.command.command_id,
    )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.COMMENT_ADDED,
        ctx.actor,
        {"annotation_id": annotation.annotation_id, "kind": annotation.kind.value},
        command_id=ctx.command.command_id,
    )
    return Decision.applied({"annotation_id": annotation.annotation_id})


async def reservation_listing(
    session: AsyncSession, episode_id: uuid.UUID | str
) -> dict[str, list[dict[str, Any]]]:
    """HELD の応答に載せる予約（§5）。provider の job 参照・session URI は載せない（INV-20）。

    - ``in_flight``: dispatch 済み・job 参照あり・未精算（完了・課金されうる）
    - ``unknown_outcome``: dispatch 済み・job 参照なし（成否不明。自動で再送も解放もしない。
      INV-15）
    - ``undispatched``: 予約済み・未送信（課金されない。resume 後に同じ冪等キーで再利用）
    """
    repo = ProviderReservationRepository(session)
    listing: dict[str, list[dict[str, Any]]] = {
        "in_flight": [],
        "unknown_outcome": [],
        "undispatched": [],
    }
    for provider in ProviderCall:
        for row in await repo.list_for_episode_provider(episode_id, provider):
            if row.status is not ReservationStatus.RESERVED:
                continue
            item = {
                "reservation_id": row.id,
                "provider": row.provider.value,
                "scene_id": row.scene_id,
                "dispatched_at": None
                if row.dispatched_at is None
                else row.dispatched_at.isoformat(),
                "estimated_cost_usd": (
                    None if row.estimated_cost_usd is None else str(row.estimated_cost_usd)
                ),
            }
            if row.dispatched_at is None:
                listing["undispatched"].append(item)
            elif row.provider_job_ref:
                listing["in_flight"].append(item)
            else:
                listing["unknown_outcome"].append(item)
    return listing


async def _hold(ctx: ApplyContext) -> Decision:
    """``hold_requested`` → ``held``。dispatch gate と同じ行 lock の下で確定する（INV-44）。

    この lock を取った時点より後に commit される dispatch は gate が拒否するので、ここで列挙した
    送信済み・成否不明が「停止の後にも完了・課金されうるもの」の全てになる。
    """
    state = ctx.control.control_state
    if state is ControlState.CANCEL_REQUESTED:
        return Decision.rejected(RejectReason.INVALID_STATE, state=state.value)
    if state is not ControlState.HELD:
        await ctx.store.update_control(
            ctx.episode_id,
            ControlChange(
                control_state=ControlState.HELD,
                held_at=ctx.now,
                hold_requested_at=ctx.control.hold_requested_at or ctx.now,
                clear_waiting=True,
            ),
        )
    listing = await reservation_listing(ctx.session, ctx.episode_id)
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.HOLD_EFFECTIVE,
        ctx.actor,
        {key: len(items) for key, items in listing.items()},
        command_id=ctx.command.command_id,
    )
    return Decision.applied(
        {
            **listing,
            "note": (
                "held: no new paid dispatch or upload starts. in_flight and unknown_outcome "
                "jobs may still complete and be charged"
            ),
        }
    )


async def _cancel(ctx: ApplyContext) -> Decision:
    """受付と記録だけ（Episode の中止は既存の人手手順。§2）。新しい dispatch は止まる。"""
    if ctx.control.control_state is not ControlState.CANCEL_REQUESTED:
        await ctx.store.update_control(
            ctx.episode_id, ControlChange(control_state=ControlState.CANCEL_REQUESTED)
        )
    return Decision.applied(
        {"note": "cancel requested and recorded; stopping the episode is a manual procedure"}
    )


async def _set_mode(ctx: ApplyContext) -> Decision:
    payload = SetModePayload.model_validate(ctx.command.payload)
    if payload.mode is InterventionMode.REVIEW_POINTS:
        if not payload.review_points:
            return Decision.rejected(
                RejectReason.INVALID_PAYLOAD, detail="review_points mode needs review_points"
            )
        points = payload.review_points
    elif payload.mode is InterventionMode.CO_PRODUCTION:
        points = payload.review_points or CO_PRODUCTION_REVIEW_POINTS
    else:
        points = ()
    await ctx.store.update_control(
        ctx.episode_id, ControlChange(mode=payload.mode, review_points=tuple(points))
    )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.MODE_CHANGED,
        ctx.actor,
        {
            "from": ctx.control.mode.value,
            "to": payload.mode.value,
            "review_points": [p.value for p in points],
        },
        command_id=ctx.command.command_id,
    )
    return Decision.applied(
        {"mode": payload.mode.value, "review_points": [p.value for p in points]}
    )


def _min_stage(stages: list[PipelineStage]) -> PipelineStage | None:
    return min(stages, key=_STAGES.index) if stages else None


async def missing_output_stage(session: AsyncSession, episode_id: str) -> PipelineStage | None:
    """現行に出力が無い最初の工程（変更の適用で降ろされた下流を含む）。全部あれば None。"""
    members = await current_members(session, episode_id)
    for stage, artifact_type in STAGE_OUTPUT:
        if manifest_slot(artifact_type) not in members:
            return stage
    return None


async def _resume(ctx: ApplyContext) -> Decision:
    """``held`` / ``hold_requested`` / ``running`` → ``running``。必要な工程から pipeline を start。

    開始工程: payload の指定、無ければ Episode 状態（ADR-0032 の ``determine_target_stage``）と
    現行の出力の欠け（変更の適用で降ろされた下流）の早い方。工程が走っている（``in_progress``）
    なら新しい pipeline は start しない（その工程が続く）。成否不明の予約が残っていれば再開しない
    （INV-15。人手の照合が先）。
    """
    payload = ResumePayload.model_validate(ctx.command.payload)
    state = ctx.control.control_state
    if state not in (ControlState.HELD, ControlState.HOLD_REQUESTED, ControlState.RUNNING):
        return Decision.rejected(RejectReason.INVALID_STATE, state=state.value)
    episode = await EpisodeRepository(ctx.session).get(ctx.episode_id)
    if episode is None:
        return Decision.rejected(RejectReason.NOT_FOUND)
    if episode.status in (EpisodeStatus.FAILED, EpisodeStatus.CANCELLED):
        return Decision.rejected(RejectReason.INVALID_STATE, episode_status=episode.status.value)
    listing = await reservation_listing(ctx.session, ctx.episode_id)
    if listing["unknown_outcome"]:
        return Decision.rejected(
            RejectReason.INVALID_STATE,
            detail="unknown-outcome reservations must be reconciled by a human first (INV-15)",
            unknown_outcome=listing["unknown_outcome"],
        )

    reason: str | None = None
    if payload.start_stage is not None:
        try:
            start: PipelineStage | None = PipelineStage(payload.start_stage)
        except ValueError:
            return Decision.rejected(RejectReason.INVALID_PAYLOAD, detail="unknown start_stage")
    elif episode.status is EpisodeStatus.IN_PROGRESS:
        start, reason = None, "a stage is running; it continues under the running state"
    else:
        owner = await EpisodeRepository(ctx.session).get_workflow_id(ctx.episode_id)
        by_status, refusal = determine_target_stage(
            status=episode.status, episode_id=ctx.episode_id, owner_workflow_id=owner
        )
        missing = await missing_output_stage(ctx.session, ctx.episode_id)
        start = _min_stage([s for s in (by_status, missing) if s is not None])
        if start is None:
            reason = refusal or "nothing to run"

    await ctx.store.update_control(
        ctx.episode_id,
        ControlChange(
            control_state=ControlState.RUNNING, clear_hold_times=True, clear_waiting=True
        ),
    )
    control_gate = ctx.control.mode in REVIEWING_MODES
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.RESUME_REQUESTED,
        ctx.actor,
        {"start_stage": None if start is None else start.value, "control_gate": control_gate},
        command_id=ctx.command.command_id,
    )
    pipeline_start = (
        None
        if start is None
        else {
            "episode_id": ctx.episode_id,
            "start_stage": start.value,
            "control_gate": control_gate,
        }
    )
    return Decision.applied(
        {
            "start_stage": None if start is None else start.value,
            "control_gate": control_gate,
            "reason": reason,
            "in_flight": listing["in_flight"],
            "undispatched": listing["undispatched"],
        },
        pipeline_start=pipeline_start,
    )


def _stage_for_point(point: Any) -> PipelineStage:
    for stage, review_point in STAGE_REVIEW_POINT.items():
        if review_point == point:
            return stage
    raise ValueError(f"no stage for review point {point}")  # pragma: no cover - 表の定義で起きない


async def _release_checkpoint(ctx: ApplyContext) -> Decision:
    """待機地点の解放。承認は**その時点の採用 manifest**に結び付ける（INV-45 の書き手側）。"""
    payload = ReleaseCheckpointPayload.model_validate(ctx.command.payload)
    control = ctx.control
    if control.control_state is not ControlState.WAITING_REVIEW:
        return Decision.rejected(RejectReason.INVALID_STATE, state=control.control_state.value)
    if control.waiting_point is not payload.point:
        return Decision.conflict(
            RejectReason.STALE_TARGET_REVISION,
            detail="the episode waits at another point",
            waiting_point=None if control.waiting_point is None else control.waiting_point.value,
        )
    if payload.manifest_id != control.active_manifest_id:
        # INV-43: 古い manifest への承認は新しい manifest へ流用しない
        return Decision.conflict(
            RejectReason.STALE_TARGET_REVISION,
            expected=payload.manifest_id,
            current=control.active_manifest_id,
        )
    approval = await ctx.store.insert_annotation(
        episode_id=ctx.episode_id,
        kind=AnnotationKind.APPROVAL,
        anchor={"manifest_id": payload.manifest_id},
        body=payload.note or f"approved at {payload.point.value}",
        actor=ctx.actor,
        payload={"point": payload.point.value},
        manifest_id=payload.manifest_id,
        command_id=ctx.command.command_id,
    )
    await ctx.store.update_control(
        ctx.episode_id, ControlChange(control_state=ControlState.RUNNING, clear_waiting=True)
    )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.CHECKPOINT_RELEASED,
        ctx.actor,
        {
            "point": payload.point.value,
            "manifest_id": payload.manifest_id,
            "annotation_id": approval.annotation_id,
        },
        command_id=ctx.command.command_id,
    )
    stage = _stage_for_point(payload.point)
    return Decision.applied(
        {
            "approval_id": approval.annotation_id,
            "start_stage": stage.value,
            "control_gate": True,
        },
        pipeline_start={
            "episode_id": ctx.episode_id,
            "start_stage": stage.value,
            "control_gate": True,
        },
    )
