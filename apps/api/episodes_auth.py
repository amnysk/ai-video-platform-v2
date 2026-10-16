"""``/episodes/*`` と ``/research/*`` の opt-in 認証（ADR-0045 §4）。

``EPISODES_API_AUTH_REQUIRED=false``（既定）なら何もしない（従来と同一）。true のとき、
GET は scope ``read``、POST は ``resume`` を持つ **人（``actor_type=human``）** の Bearer principal
を要求する（POST は有料生成・YouTube 投稿に繋がるため。``ai_assistant``/``agent`` は 403
``forbidden`` reason=``human_only``）。principal の
照合・エラーの形は介入 API と同じ（``apps.api.routers.intervention``）。新しい認証方式は作らない。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from apps.api.auth import PrincipalRegistry
from apps.api.dependencies import get_settings
from apps.api.routers.intervention import (
    _missing_scope,  # pyright: ignore[reportPrivateUsage]
    current_principal,
    get_principal_registry,
)
from contracts.application import OperationErrorCode
from contracts.intervention import ActorType, Scope
from infrastructure.config import Settings

_READ_METHODS = ("GET", "HEAD", "OPTIONS")


def _scope_for(method: str) -> Scope:
    return Scope.READ if method in _READ_METHODS else Scope.RESUME


def get_episodes_registry(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PrincipalRegistry | None:
    """OFF のときは registry を読まない（設定の誤りが /episodes を壊さない）。

    ON のときは介入 API と同じ registry（同じ読み込み・同じ 503）。テストはこの関数を差し替える。
    """
    if not settings.episodes_api_auth_required:
        return None
    return get_principal_registry(settings)


async def require_episodes_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    registry: Annotated[PrincipalRegistry | None, Depends(get_episodes_registry)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.episodes_api_auth_required:
        return
    principal = await current_principal(registry, authorization)
    scope = _scope_for(request.method)
    if not principal.has(scope):
        raise _missing_scope(scope)
    if request.method not in _READ_METHODS and principal.actor_type is not ActorType.HUMAN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": OperationErrorCode.FORBIDDEN.value,
                "reason": "human_only",
                "message": "this endpoint can only be requested by a human",
            },
        )
