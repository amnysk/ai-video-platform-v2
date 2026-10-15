"""介入 API（/v1/intervention）。JP-MAS-001 v2.0 §10・ADR-0041 §8・INV-47。

HTTP の形・使い方は ``docs/operations/intervention-api.md``。
契約は ``docs/domain/intervention.md``。

- **既定で閉じる**: ``INTERVENTION_PRINCIPALS`` が未設定なら全 route が
  ``503 intervention_disabled``。
  読み込めない設定（AI に人専用 scope 等）は ``503 intervention_misconfigured``。
- 認証は ``Authorization: Bearer <token>``。token は sha256 で照合するだけで、保存もログもしない。
- 読み取りは ``read`` scope、command は ``REQUIRED_SCOPE[type]``（``contracts/intervention.py``）。
- **予算上限・Schedule・公開設定を変える route・command 型は無い**（INV-47）。未知の型は
  ``422 unsupported_command``、payload の余分な項目は ``422 invalid_payload``。
- handler は検証 → 保存（commit）→ workflow の start（best-effort）→ 返す、だけ。
  適用の完了は待たない（INV-16）。``received`` を「完了」と表示しない。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from starlette.requests import Request

from apps.api.auth import ApiPrincipal, PrincipalConfigError, PrincipalRegistry, load_principals
from apps.api.dependencies import get_session_factory, get_settings
from apps.api.intervention_backend import (
    CommandIdReusedError,
    CommandRejectedError,
    EpisodeNotFoundError,
    InterventionBackend,
    ModulesUnavailableError,
    ProposalFileError,
    Received,
    RevisionNotFoundError,
    RevisionTypeMismatchError,
    StoreBackedInterventionBackend,
    load_real_modules,
)
from contracts.intervention import (
    PAYLOAD_MODELS,
    REQUIRED_SCOPE,
    TERMINAL_COMMAND_STATUSES,
    ChangeSetStatus,
    CommandEnvelope,
    CommandStatus,
    CommandType,
    RejectReason,
    Scope,
)
from infrastructure.config import Settings
from infrastructure.intervention.store import ChangeSet, Command

logger = logging.getLogger("avp.api.intervention")

PREFIX = "/v1/intervention"


#: B の受付拒否（``RejectReason``）→ HTTP status。**競合（期待版・状態）は 409**、権限は 403、
#: それ以外（形・制約・予算）は 422。未知の code は 422 ``command_rejected``。
_REJECT_STATUS: dict[str, int] = {
    RejectReason.STALE_CONTROL_REVISION.value: status.HTTP_409_CONFLICT,
    RejectReason.STALE_TARGET_REVISION.value: status.HTTP_409_CONFLICT,
    RejectReason.INVALID_STATE.value: status.HTTP_409_CONFLICT,
    RejectReason.FORBIDDEN.value: status.HTTP_403_FORBIDDEN,
    RejectReason.UNSUPPORTED_COMMAND.value: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RejectReason.UNSUPPORTED_CHANGE.value: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RejectReason.INVALID_PAYLOAD.value: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RejectReason.USER_CONSTRAINT_VIOLATION.value: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RejectReason.BUDGET_EXCEEDED.value: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RejectReason.BUDGET_UNVERIFIABLE.value: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _backend_error(exc: Exception) -> HTTPException | None:
    """backend の例外を HTTP のエラーに写す。例外の文言・入力値は返さない（ログにも出さない）。"""
    if isinstance(exc, ModulesUnavailableError):
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "intervention_unavailable",
            "the intervention backend is temporarily unavailable; retry later",
        )
    if isinstance(exc, EpisodeNotFoundError):
        return _error(status.HTTP_404_NOT_FOUND, "episode_not_found", "episode not found")
    if isinstance(exc, RevisionTypeMismatchError):
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "revision_type_mismatch",
            "the two revisions cannot be compared",
        )
    if isinstance(exc, CommandRejectedError):
        if exc.code in _REJECT_STATUS:
            return _error(_REJECT_STATUS[exc.code], exc.code, "the command was rejected")
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "command_rejected", "the command was rejected"
        )
    return None


class _InterventionRoute(APIRoute):
    """全 route 共通: backend の失敗を機械可読な HTTP エラーにする（未対応の例外は 500 のまま）。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except Exception as exc:
                mapped = _backend_error(exc)
                if mapped is None:
                    raise
                raise mapped from None

        return handler


router = APIRouter(prefix=PREFIX, tags=["intervention"], route_class=_InterventionRoute)

#: command の状態の表示。**APPLIED 以外は「未適用」と書く**（RECEIVED を完了と表示しない）
STATUS_TEXT: dict[CommandStatus, str] = {
    CommandStatus.RECEIVED: "受付済み・未適用（適用は非同期。status で確認する）",
    CommandStatus.VALIDATED: "検証済み・未適用（適用待ち）",
    CommandStatus.APPLYING: "適用中・未適用（まだ反映されていない）",
    CommandStatus.APPLIED: "適用済み（変更は記録された。下流の再生成・完成動画は別に進む）",
    CommandStatus.CONFLICT: "競合・未適用（期待版が現在と違う。再取得して出し直す）",
    CommandStatus.REJECTED: "却下・未適用（reject_reason を見る）",
    CommandStatus.FAILED: "失敗・未適用（reject_reason と result を見る）",
}

_SUPPORTED_TYPES = frozenset(t.value for t in CommandType)


# --------------------------------------------------------------------------- エラー


def _error(code: int, error: str, message: str, **extra: Any) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if code == status.HTTP_401_UNAUTHORIZED else None
    return HTTPException(
        status_code=code, detail={"error": error, "message": message, **extra}, headers=headers
    )


def _missing_scope(scope: Scope) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "missing_scope",
            "required_scope": scope.value,
            "message": f"this principal lacks the {scope.value!r} scope",
        },
    )


# --------------------------------------------------------------------------- 依存


@lru_cache(maxsize=4)
def _registry_for(raw: str | None) -> PrincipalRegistry | None:
    return load_principals(raw)


def get_principal_registry(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PrincipalRegistry | None:
    secret = settings.intervention_principals
    try:
        return _registry_for(None if secret is None else secret.get_secret_value())
    except PrincipalConfigError as exc:
        # 文言は secret・hash を含まない（apps/api/auth.py）
        logger.error("intervention API disabled: %s", exc)
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "intervention_misconfigured",
            "INTERVENTION_PRINCIPALS cannot be loaded; the intervention API is disabled",
        ) from None


async def current_principal(
    registry: Annotated[PrincipalRegistry | None, Depends(get_principal_registry)],
    authorization: Annotated[str | None, Header()] = None,
) -> ApiPrincipal:
    if registry is None:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "intervention_disabled",
            "the intervention API is disabled (INTERVENTION_PRINCIPALS is not configured)",
        )
    scheme, _, token = (authorization or "").partition(" ")
    principal = (
        registry.authenticate(token.strip())
        if scheme.lower() == "bearer" and token.strip()
        else None
    )
    if principal is None:
        raise _error(
            status.HTTP_401_UNAUTHORIZED, "unauthenticated", "a valid Bearer token is required"
        )
    return principal


def _require(scope: Scope) -> Callable[..., Coroutine[Any, Any, ApiPrincipal]]:
    async def dependency(
        principal: Annotated[ApiPrincipal, Depends(current_principal)],
    ) -> ApiPrincipal:
        if not principal.has(scope):
            raise _missing_scope(scope)
        return principal

    return dependency


@lru_cache(maxsize=1)
def _real_backend() -> StoreBackedInterventionBackend:  # pragma: no cover - 実接続経路
    settings = get_settings()
    session_factory = get_session_factory()
    return StoreBackedInterventionBackend(
        session_factory, load_real_modules(settings, session_factory)
    )


def get_intervention_backend() -> InterventionBackend:  # pragma: no cover - 実接続経路
    """テストは override する。認証の後に解決される（handler の引数順）。"""
    return _real_backend()


Reader = Annotated[ApiPrincipal, Depends(_require(Scope.READ))]
Authenticated = Annotated[ApiPrincipal, Depends(current_principal)]
Backend = Annotated[InterventionBackend, Depends(get_intervention_backend)]


async def _require_episode(backend: InterventionBackend, episode_id: uuid.UUID) -> str:
    key = str(episode_id)
    if not await backend.episode_exists(key):
        raise _error(status.HTTP_404_NOT_FOUND, "episode_not_found", f"episode {key} not found")
    return key


# --------------------------------------------------------------------------- 表示の形


class ActorView(BaseModel):
    type: str
    id: str


class CommandView(BaseModel):
    command_id: str
    episode_id: str
    type: CommandType
    status: CommandStatus
    #: 人向けの説明。APPLIED 以外は「未適用」
    status_text: str
    #: ``status == applied`` のときだけ True
    applied: bool
    terminal: bool
    actor: ActorView
    granted_scope: Scope
    expected_control_revision: int | None
    expected_target_revision: str | None
    payload: dict[str, Any]
    reject_reason: str | None
    result: dict[str, Any] | None
    received_at: datetime
    finished_at: datetime | None
    dispatched: bool


class ImportedCommand(BaseModel):
    command_id: str
    status: CommandStatus
    status_text: str
    #: False = 同じ内容の取り込み済み command（新しく作っていない）
    created: bool


class ImportResult(BaseModel):
    commands: list[ImportedCommand]


class ChangeSetView(BaseModel):
    change_set_id: str
    episode_id: str
    kind: str
    status: ChangeSetStatus
    base_revision_id: str
    base_manifest_id: str | None
    #: 変更案（差分の内容）
    proposal: dict[str, Any]
    #: 影響範囲（stale・再生成・再利用）。未計算なら None
    impact: dict[str, Any] | None
    #: 見積り（USD）。**未見積りは None**（0 として表示しない）
    estimated_cost_usd: float | None
    cost_estimated: bool
    rationale: str | None
    variant_group: str | None
    applied_revision_id: str | None
    applied_manifest_id: str | None
    proposed_by_command_id: str
    decided_by_command_id: str | None
    actor: ActorView
    created_at: datetime


def _command_view(command: Command) -> CommandView:
    return CommandView(
        command_id=command.command_id,
        episode_id=command.episode_id,
        type=command.type,
        status=command.status,
        status_text=STATUS_TEXT[command.status],
        applied=command.status is CommandStatus.APPLIED,
        terminal=command.status in TERMINAL_COMMAND_STATUSES,
        actor=ActorView(type=command.actor.type.value, id=command.actor.id),
        granted_scope=command.granted_scope,
        expected_control_revision=command.expected_control_revision,
        expected_target_revision=command.expected_target_revision,
        payload=command.payload,
        reject_reason=None if command.reject_reason is None else command.reject_reason.value,
        result=command.result,
        received_at=command.received_at,
        finished_at=command.finished_at,
        dispatched=command.dispatched_at is not None,
    )


def _change_set_view(change_set: ChangeSet) -> ChangeSetView:
    cost: Decimal | None = change_set.estimated_cost_usd
    return ChangeSetView(
        change_set_id=change_set.change_set_id,
        episode_id=change_set.episode_id,
        kind=change_set.kind.value,
        status=change_set.status,
        base_revision_id=change_set.base_revision_id,
        base_manifest_id=change_set.base_manifest_id,
        proposal=change_set.proposal,
        impact=change_set.impact,
        estimated_cost_usd=None if cost is None else float(cost),
        cost_estimated=cost is not None,
        rationale=change_set.rationale,
        variant_group=change_set.variant_group,
        applied_revision_id=change_set.applied_revision_id,
        applied_manifest_id=change_set.applied_manifest_id,
        proposed_by_command_id=change_set.proposed_by_command_id,
        decided_by_command_id=change_set.decided_by_command_id,
        actor=ActorView(type=change_set.actor.type.value, id=change_set.actor.id),
        created_at=change_set.created_at,
    )


# --------------------------------------------------------------------------- 検証


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    # 入力値は返さない（payload に何が入っていても応答・ログへ写さない）
    return [
        {"loc": [str(p) for p in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_input=False)
    ]


def _invalid(message: str, errors: list[dict[str, Any]] | None = None) -> HTTPException:
    extra: dict[str, Any] = {} if errors is None else {"errors": errors}
    return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_payload", message, **extra)


def _parse_envelope(body: Any) -> CommandEnvelope:
    if not isinstance(body, dict):
        raise _invalid("the request body must be a JSON object (CommandEnvelope)")
    if "type" in body and body["type"] not in _SUPPORTED_TYPES:
        # INV-47: 予算・Schedule・公開設定などを変える型は語彙に無い
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "unsupported_command",
            "this command type is not supported by the intervention API",
            supported_types=sorted(_SUPPORTED_TYPES),
        )
    try:
        envelope = CommandEnvelope.model_validate(body)
    except ValidationError as exc:
        raise _invalid("invalid CommandEnvelope", _validation_errors(exc)) from None
    _validate_payload(envelope.type, envelope.payload)
    return envelope


def _validate_payload(command_type: CommandType, payload: dict[str, Any]) -> None:
    try:
        PAYLOAD_MODELS[command_type].model_validate(payload)
    except ValidationError as exc:
        raise _invalid(
            f"invalid payload for {command_type.value}", _validation_errors(exc)
        ) from None


def proposal_command_id(episode_id: str, proposal: dict[str, Any], occurrence: int) -> str:
    """取り込む変更案の決定的な command_id（同じ案の再取り込みは同じ id = 二重にしない）。

    ``occurrence`` は同じファイル内で同じ内容の案が何個目か（並び順には依らない）。
    """
    canonical = json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(f"{episode_id}\n{canonical}\n{occurrence}".encode()).hexdigest()
    return f"cp-{digest[:56]}"


async def _start_new(backend: InterventionBackend, received: Sequence[Received]) -> None:
    """commit 後の配送。未配送で終端でない command だけ start する（失敗は backend がログ）。"""
    for item in received:
        command = item.command
        if command.status in TERMINAL_COMMAND_STATUSES:
            continue
        if item.created or command.dispatched_at is None:
            await backend.start_command(command.command_id)


# --------------------------------------------------------------------------- 読み取り


@router.get("/episodes/{episode_id}")
async def get_episode_view(episode_id: uuid.UUID, _: Reader, backend: Backend) -> Any:
    key = await _require_episode(backend, episode_id)
    return jsonable_encoder(await backend.load_view(key))


@router.get("/episodes/{episode_id}/decisions")
async def list_decisions(episode_id: uuid.UUID, _: Reader, backend: Backend) -> dict[str, Any]:
    key = await _require_episode(backend, episode_id)
    return {"decisions": jsonable_encoder(await backend.list_decisions(key))}


@router.get("/episodes/{episode_id}/revisions/compare")
async def compare_revisions(
    episode_id: uuid.UUID,
    _: Reader,
    backend: Backend,
    a: Annotated[uuid.UUID, Query(description="比較元の revision_id")],
    b: Annotated[uuid.UUID, Query(description="比較先の revision_id")],
) -> Any:
    key = await _require_episode(backend, episode_id)
    try:
        return jsonable_encoder(await backend.compare(key, str(a), str(b)))
    except RevisionNotFoundError:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "revision_not_found",
            "a revision does not exist or does not belong to this episode",
        ) from None


@router.get("/episodes/{episode_id}/review-packet")
async def get_review_packet(
    episode_id: uuid.UUID,
    _: Reader,
    backend: Backend,
    format: Annotated[Literal["json", "markdown", "html"], Query()] = "json",  # noqa: A002
) -> Response:
    key = await _require_episode(backend, episode_id)
    no_store = {"Cache-Control": "no-store"}
    if format == "json":
        return JSONResponse(jsonable_encoder(await backend.packet(key)), headers=no_store)
    markdown = await backend.packet_markdown(key)
    if format == "markdown":
        return PlainTextResponse(
            markdown, media_type="text/markdown; charset=utf-8", headers=no_store
        )
    # 読み取り専用の簡易表示。Markdown はエスケープして <pre> に入れるだけ
    # （スクリプトを実行させない）
    page = (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        f"<title>ReviewPacket {html.escape(key)}</title>"
        "<style>body{font-family:sans-serif;margin:1rem}pre{white-space:pre-wrap}</style>"
        f"</head><body><pre>{html.escape(markdown)}</pre></body></html>"
    )
    return HTMLResponse(
        page,
        headers={
            **no_store,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/episodes/{episode_id}/commands")
async def list_episode_commands(
    episode_id: uuid.UUID, _: Reader, backend: Backend
) -> dict[str, list[CommandView]]:
    key = await _require_episode(backend, episode_id)
    return {"commands": [_command_view(c) for c in await backend.list_commands(key)]}


@router.get("/commands/{command_id}", response_model=CommandView)
async def get_command_status(command_id: str, _: Reader, backend: Backend) -> CommandView:
    command = await backend.get_command(command_id)
    if command is None:
        raise _error(
            status.HTTP_404_NOT_FOUND, "command_not_found", f"command {command_id} not found"
        )
    return _command_view(command)


@router.get("/episodes/{episode_id}/change-sets")
async def list_change_sets(
    episode_id: uuid.UUID, _: Reader, backend: Backend
) -> dict[str, list[ChangeSetView]]:
    key = await _require_episode(backend, episode_id)
    return {"change_sets": [_change_set_view(c) for c in await backend.list_change_sets(key)]}


@router.get("/episodes/{episode_id}/change-sets/{change_set_id}", response_model=ChangeSetView)
async def get_change_set(
    episode_id: uuid.UUID, change_set_id: uuid.UUID, _: Reader, backend: Backend
) -> ChangeSetView:
    key = await _require_episode(backend, episode_id)
    change_set = await backend.get_change_set(str(change_set_id))
    if change_set is None or change_set.episode_id != key:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "change_set_not_found",
            f"change set {change_set_id} not found in this episode",
        )
    return _change_set_view(change_set)


# --------------------------------------------------------------------------- command


@router.post(
    "/episodes/{episode_id}/commands",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandView,
    responses={200: {"description": "同じ command_id・同じ内容の再送（保存済みの状態）"}},
)
async def post_command(
    episode_id: uuid.UUID,
    principal: Authenticated,
    backend: Backend,
    response: Response,
    body: Annotated[Any, Body()] = None,
) -> CommandView:
    envelope = _parse_envelope(body)
    required = REQUIRED_SCOPE[envelope.type]
    if not principal.has(required):
        raise _missing_scope(required)
    try:
        [received] = await backend.receive(str(episode_id), [envelope], principal)
    except EpisodeNotFoundError:
        raise _error(
            status.HTTP_404_NOT_FOUND, "episode_not_found", f"episode {episode_id} not found"
        ) from None
    except CommandIdReusedError:
        raise _error(
            status.HTTP_409_CONFLICT,
            "command_id_reused",
            "this command_id was already used with a different payload; use a new command_id",
        ) from None
    await _start_new(backend, [received])
    if not received.created:
        response.status_code = status.HTTP_200_OK
    return _command_view(received.command)


@router.post(
    "/episodes/{episode_id}/change-proposals",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportResult,
    responses={200: {"description": "全ての案が取り込み済み（新しい command なし）"}},
)
async def import_change_proposals(
    episode_id: uuid.UUID,
    principal: Annotated[ApiPrincipal, Depends(_require(Scope.PROPOSE))],
    backend: Backend,
    response: Response,
    body: Annotated[Any, Body()] = None,
) -> ImportResult:
    """ReviewPacket から作った変更案ファイル（``ChangeProposalFile``）を propose_change にする。"""
    key = str(episode_id)
    if not isinstance(body, dict):
        raise _invalid("the request body must be a ChangeProposalFile JSON object")
    try:
        parsed = backend.parse_proposal_file(body)
    except (ProposalFileError, ValidationError):
        raise _invalid("invalid ChangeProposalFile") from None
    if parsed.episode_id is not None and parsed.episode_id != key:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "episode_mismatch",
            "the proposal file belongs to another episode",
        )
    if not parsed.proposals:
        raise _invalid("the proposal file has no proposals")
    seen: dict[str, int] = {}
    envelopes: list[CommandEnvelope] = []
    for proposal in parsed.proposals:
        _validate_payload(CommandType.PROPOSE_CHANGE, proposal)
        canonical = json.dumps(proposal, sort_keys=True, ensure_ascii=False)
        seen[canonical] = seen.get(canonical, 0) + 1
        envelopes.append(
            CommandEnvelope(
                command_id=proposal_command_id(key, proposal, seen[canonical]),
                type=CommandType.PROPOSE_CHANGE,
                payload=proposal,
            )
        )
    try:
        received = await backend.receive(key, envelopes, principal)
    except EpisodeNotFoundError:
        raise _error(
            status.HTTP_404_NOT_FOUND, "episode_not_found", f"episode {key} not found"
        ) from None
    except CommandIdReusedError:
        raise _error(
            status.HTTP_409_CONFLICT,
            "command_id_reused",
            "a derived command_id is already used with a different payload",
        ) from None
    await _start_new(backend, received)
    if not any(r.created for r in received):
        response.status_code = status.HTTP_200_OK
    return ImportResult(
        commands=[
            ImportedCommand(
                command_id=r.command.command_id,
                status=r.command.status,
                status_text=STATUS_TEXT[r.command.status],
                created=r.created,
            )
            for r in received
        ]
    )
