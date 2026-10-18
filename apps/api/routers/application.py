"""Application 層の HTTP 入口（ADR-0043 §1、ADR-0044 §4）。route は Application Service を呼ぶだけ。

- route は ``contracts.application_operations.OPERATIONS`` から登録する（method・path・request/
  response model を手で再掲しない / INV-52）。handler は 認証 → Caller 作成 → service 呼び出し →
  ``OperationError`` の写像、だけ。scope・人限定の検査と観測は service が行う。

- ``JPMAS2_APPLICATION_API=false``（既定）なら ``404 feature_disabled``（他の何よりも先）。
- 認証は介入 API と同じ依存（``current_principal``）。
- ``OperationErrorCode`` → HTTP status の写像はこの module の ``ERROR_STATUS`` 1 か所。
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel

from application.errors import OperationError, OperationErrorCode
from apps.api.application_adapters import caller_from_principal
from apps.api.application_service import ApplicationServicePort, get_application_service
from apps.api.auth import ApiPrincipal
from apps.api.dependencies import get_settings
from apps.api.intervention_logging import (
    _SAFE_CORRELATION,  # pyright: ignore[reportPrivateUsage]
)
from apps.api.routers.intervention import (
    PREFIX,
    _InterventionRoute,  # pyright: ignore[reportPrivateUsage]
    current_principal,
)
from contracts.application import StartAgenticProductionResult, StoryboardProposalResult
from contracts.application_operations import (
    OPERATIONS,
    OperationName,
    OperationSpec,
)
from contracts.assistant_tools import CORRELATION_HEADER
from infrastructure.config import Settings

#: 操作の失敗 → HTTP status。**競合・再利用は 409**、権限は 403、未検出・機能無効は 404、他は 422
ERROR_STATUS: dict[OperationErrorCode, int] = {
    OperationErrorCode.COMMAND_ID_REUSED: status.HTTP_409_CONFLICT,
    OperationErrorCode.STALE_CONTROL_REVISION: status.HTTP_409_CONFLICT,
    OperationErrorCode.STALE_TARGET_REVISION: status.HTTP_409_CONFLICT,
    OperationErrorCode.MISSING_SCOPE: status.HTTP_403_FORBIDDEN,
    OperationErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    OperationErrorCode.EPISODE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperationErrorCode.CHANGE_SET_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperationErrorCode.INVALID_STATE: status.HTTP_409_CONFLICT,
    OperationErrorCode.FEATURE_DISABLED: status.HTTP_404_NOT_FOUND,
    OperationErrorCode.TOOL_CALL_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    #: 保存された結果が記録と合わない。クライアントの誤りではない（黙って返さず 500）
    OperationErrorCode.INTEGRITY_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}
_DEFAULT_ERROR_STATUS = status.HTTP_422_UNPROCESSABLE_CONTENT


def operation_error_to_http(exc: OperationError) -> HTTPException:
    return HTTPException(
        status_code=ERROR_STATUS.get(exc.code, _DEFAULT_ERROR_STATUS),
        detail={"error": exc.code.value, "message": exc.message, **exc.detail},
    )


def require_application_api(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.jpmas2_application_api:
        raise operation_error_to_http(
            OperationError(
                OperationErrorCode.FEATURE_DISABLED,
                "the application API is disabled (JPMAS2_APPLICATION_API)",
            )
        )


def _require_flag(flag: str) -> Callable[..., None]:
    """``OperationSpec.feature_flag`` の ``Settings`` 項目が無効なら 404 feature_disabled。"""

    def check(settings: Annotated[Settings, Depends(get_settings)]) -> None:
        if not getattr(settings, flag):
            raise operation_error_to_http(
                OperationError(
                    OperationErrorCode.FEATURE_DISABLED,
                    f"this operation is disabled ({flag.upper()})",
                )
            )

    return check


router = APIRouter(
    prefix=PREFIX,
    tags=["application"],
    route_class=_InterventionRoute,
    dependencies=[Depends(require_application_api)],
)


def _correlation_id(header: str | None) -> str:
    """入口が付ける相関 ID。安全な ``X-Correlation-ID`` はそのまま、無い・不正なら新しく作る。"""
    if header is not None and _SAFE_CORRELATION.fullmatch(header):
        return header
    return str(uuid.uuid4())


def _is_new(result: BaseModel) -> bool:
    """今回の呼び出しで新しく受け付けた（作った・始めた）か。False = 同じ内容の再送。"""
    if isinstance(result, StoryboardProposalResult):
        return any(c.created for c in result.commands)
    if isinstance(result, StartAgenticProductionResult):
        return result.started
    return bool(getattr(result, "created", True))


def _endpoint(spec: OperationSpec) -> Callable[..., Awaitable[Any]]:
    """カタログの 1 操作の handler。引数は path・body・query で決まる（signature を組み立てる）。"""
    has_episode = "{episode_id}" in spec.http_path
    has_change_set = "{change_set_id}" in spec.http_path
    has_tool_call = "{tool_call_id}" in spec.http_path
    posts = spec.http_method == "POST"
    method_name = spec.name.value

    async def endpoint(**kw: Any) -> Any:
        service: ApplicationServicePort = kw["service"]
        caller = caller_from_principal(kw["principal"], _correlation_id(kw["correlation"]))
        args: list[Any] = [
            kw[n] for n in ("episode_id", "change_set_id", "tool_call_id", "payload") if n in kw
        ]
        try:
            result = await getattr(service, method_name)(caller, *args)
        except OperationError as exc:
            raise operation_error_to_http(exc) from None
        if posts and not _is_new(result):
            kw["response"].status_code = status.HTTP_200_OK
        return result

    k = inspect.Parameter.KEYWORD_ONLY
    params = [
        inspect.Parameter(
            "principal", k, annotation=Annotated[ApiPrincipal, Depends(current_principal)]
        ),
        inspect.Parameter(
            "service",
            k,
            annotation=Annotated[ApplicationServicePort, Depends(get_application_service)],
        ),
        inspect.Parameter("response", k, annotation=Response),
        inspect.Parameter(
            "correlation",
            k,
            annotation=Annotated[str | None, Header(alias=CORRELATION_HEADER)],
            default=None,
        ),
    ]
    if has_episode:
        params.append(inspect.Parameter("episode_id", k, annotation=uuid.UUID))
    if has_change_set:
        params.append(inspect.Parameter("change_set_id", k, annotation=str))
    if has_tool_call:
        params.append(inspect.Parameter("tool_call_id", k, annotation=str))
    if spec.query_model is not None:
        params.append(
            inspect.Parameter("payload", k, annotation=Annotated[spec.query_model, Query()])
        )
    elif spec.request_model is not None:
        params.append(
            inspect.Parameter("payload", k, annotation=Annotated[spec.request_model, Body()])
        )
    endpoint.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    endpoint.__name__ = method_name
    return endpoint


def _register(spec: OperationSpec) -> None:
    created = spec.name is OperationName.CREATE_EPISODE_DRAFT
    responses: dict[int | str, dict[str, Any]] = {}
    if spec.http_method == "POST":
        responses[200] = {"description": "同じ内容の再送（新しい command・Episode・workflow なし）"}
    router.add_api_route(
        spec.http_path,
        _endpoint(spec),
        methods=[spec.http_method],
        response_model=spec.result_model,
        status_code=(
            status.HTTP_201_CREATED
            if created
            else status.HTTP_202_ACCEPTED
            if spec.http_method == "POST"
            else status.HTTP_200_OK
        ),
        responses=responses or None,
        dependencies=(
            None if spec.feature_flag is None else [Depends(_require_flag(spec.feature_flag))]
        ),
        operation_id=spec.name.value,
        summary=spec.name.value,
    )


for _spec in OPERATIONS:
    _register(_spec)
