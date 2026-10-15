"""Application 層の HTTP 入口（ADR-0043 §1）。route は use case を呼ぶだけ。

- ``JPMAS2_APPLICATION_API=false``（既定）なら ``404 feature_disabled``（他の何よりも先）。
- 認証は介入 API と同じ依存（``current_principal``）。
- ``OperationErrorCode`` → HTTP status の写像はこの module の ``ERROR_STATUS`` 1 か所。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from application.errors import OperationError, OperationErrorCode
from application.use_cases.propose_storyboard_change import ProposeStoryboardChange
from apps.api.application_adapters import BackendCommandPort, caller_from_principal
from apps.api.auth import ApiPrincipal
from apps.api.dependencies import get_settings
from apps.api.routers.intervention import (
    PREFIX,
    Backend,
    _InterventionRoute,  # pyright: ignore[reportPrivateUsage]
    current_principal,
)
from contracts.application import ProposeStoryboardChangeRequest, StoryboardProposalResult
from infrastructure.config import Settings

#: 操作の失敗 → HTTP status。**競合・再利用は 409**、権限は 403、未検出・機能無効は 404、他は 422
ERROR_STATUS: dict[OperationErrorCode, int] = {
    OperationErrorCode.COMMAND_ID_REUSED: status.HTTP_409_CONFLICT,
    OperationErrorCode.STALE_CONTROL_REVISION: status.HTTP_409_CONFLICT,
    OperationErrorCode.STALE_TARGET_REVISION: status.HTTP_409_CONFLICT,
    OperationErrorCode.MISSING_SCOPE: status.HTTP_403_FORBIDDEN,
    OperationErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    OperationErrorCode.EPISODE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperationErrorCode.FEATURE_DISABLED: status.HTTP_404_NOT_FOUND,
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


router = APIRouter(
    prefix=PREFIX,
    tags=["application"],
    route_class=_InterventionRoute,
    dependencies=[Depends(require_application_api)],
)


@router.post(
    "/episodes/{episode_id}/storyboard/proposals",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StoryboardProposalResult,
    responses={200: {"description": "全て同じ内容の再送（新しい command なし）"}},
)
async def propose_storyboard_change(
    episode_id: uuid.UUID,
    body: ProposeStoryboardChangeRequest,
    principal: Annotated[ApiPrincipal, Depends(current_principal)],
    backend: Backend,
    response: Response,
) -> Any:
    use_case = ProposeStoryboardChange(BackendCommandPort(backend))
    try:
        result = await use_case.execute(caller_from_principal(principal), episode_id, body)
    except OperationError as exc:
        raise operation_error_to_http(exc) from None
    if not any(c.created for c in result.commands):
        response.status_code = status.HTTP_200_OK
    return result
