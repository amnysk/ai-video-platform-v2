"""API の変更系ルートの認証（ADR-0080 §B / INV-124）。

- 変更系（POST / PUT / PATCH / DELETE）は ``Authorization: Bearer <token>`` を要求する。
  読み取り（GET 等）は開けたまま（運用者・監視・healthcheck が使い、データの変更・課金・投稿を
  起こさない）。
- トークンは repo 外の 0600 のファイル（``Settings.api_tokens_file`` = ``AVP_API_TOKENS_PATH``）から
  読む。1 行 1 件 ``owner:<token>`` / ``agent:<token>``（空行と ``#`` 行は無視）。32 文字以上。
- **未設定・読めない・壊れている・他者が読める権限なら、変更系を全拒否（503）**。
  誤設定で無認証に倒れない。
- 比較は sha256 の digest を ``hmac.compare_digest`` で全件分行う（打ち切らない）。
  token は保存・ログに出さない。
- 認証は actor（``owner`` / ``agent``）を返し、変更系の構造化ログに残す。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from apps.api.dependencies import get_settings
from contracts.approval import API_AUTH_MODE_ENV, ApiAuthMode, ApproverKind
from contracts.log_contract import EventName
from domain.quality.approval import UploadApprover
from infrastructure.api_tokens import TOKEN_MIN_LENGTH, read_token_file
from infrastructure.config import Settings
from infrastructure.logging.emit import emit, log_guard

logger = logging.getLogger("avp.api.auth")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_BEARER = "Bearer "


class Actor(StrEnum):
    """変更系リクエストの実行者の種別。所有者本人 / エージェント。"""

    OWNER = "owner"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class ApiTokens:
    """token そのものは持たず、sha256 の digest だけを持つ。"""

    entries: tuple[tuple[Actor, bytes], ...] = ()

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[Actor, str]]) -> ApiTokens:
        return cls(
            tuple((actor, hashlib.sha256(token.encode()).digest()) for actor, token in pairs)
        )

    def is_empty(self) -> bool:
        return not self.entries


def _refuse(reason: str) -> ApiTokens:
    with log_guard():
        emit(
            logger,
            EventName.LOG_RECORD,
            logging.ERROR,
            "api token file rejected: %s; mutating routes stay closed",
            reason,
        )
    return ApiTokens()


def load_api_tokens(path: str | None, owner_path: str | None = None) -> ApiTokens:
    """token ファイルを読む。どんな不備でも例外にせず、空（= 未設定 = 全拒否）を返す。

    ``owner_path``（``AVP_API_OWNER_TOKEN_PATH``）を設定すると、owner token は
    そのファイルからだけ読む。
    その場合、通常のファイルに ``owner:`` 行があれば拒否する（owner token をエージェントが読める
    ファイルに残さない / ADR-0080 §B）。owner ファイルが設定されているのに読めないときも全拒否。
    """
    main = read_token_file(path)
    if not path:
        return ApiTokens()
    if main.problem is not None:
        return _refuse(main.problem) if main.problem != "absent" else ApiTokens()
    pairs = [(Actor(name), token) for name, token in main.entries]
    if owner_path:
        owner = read_token_file(owner_path)
        if owner.problem is not None:
            return _refuse(f"owner token file: {owner.problem}")
        if any(actor is Actor.OWNER for actor, _t in pairs):
            return _refuse("owner token found in the shared file while an owner file is configured")
        if any(name != "owner" for name, _t in owner.entries):
            return _refuse("owner token file may hold only owner tokens")
        pairs += [(Actor(name), token) for name, token in owner.entries]
    if len({t for _a, t in pairs}) != len(pairs):
        return _refuse("a token is used twice")
    return ApiTokens.from_pairs(pairs)


def authenticate(authorization: str | None, tokens: ApiTokens) -> Actor | None:
    """``Bearer <token>`` を検証して actor を返す。全件を定数時間で比較する。"""
    if not authorization or not authorization.startswith(_BEARER):
        return None
    presented = hashlib.sha256(authorization[len(_BEARER) :].encode()).digest()
    matched: Actor | None = None
    for actor, digest in tokens.entries:
        if hmac.compare_digest(presented, digest) and matched is None:
            matched = actor
    return matched


TokenSource = Callable[[], ApiTokens]


def get_api_token_source(settings: Annotated[Settings, Depends(get_settings)]) -> TokenSource:
    """token を読む関数を返す。**読むのは変更系のときだけ**。

    GET ではファイルに触れず、ログも出さない。

    呼ぶたびにファイルを読む（鍵の差し替えが再起動なしで効く。小さなファイル）。
    """
    return lambda: load_api_tokens(settings.api_tokens_file, settings.api_owner_token_file)


async def require_api_actor(
    request: Request,
    source: Annotated[TokenSource, Depends(get_api_token_source)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Actor | None:
    """変更系ルートの認証。読み取りは通す（None）。未設定は 503、不正は 401。

    ``AVP_API_AUTH_MODE=disabled``（所有者の明示。ADR-0080 追補）なら token ファイルを読まず通す。
    actor は None なので ``get_upload_approver`` も None（REVIEW は解放されない）。
    """
    if request.method in SAFE_METHODS:
        return None
    if settings.api_auth_mode is ApiAuthMode.DISABLED:
        with log_guard():
            emit(
                logger,
                EventName.LOG_RECORD,
                logging.INFO,
                "api authentication disabled by %s=disabled: %s %s",
                API_AUTH_MODE_ENV,
                request.method,
                request.url.path,
                attributes={"auth_mode": ApiAuthMode.DISABLED.value, "method": request.method},
            )
        return None
    tokens = source()
    if tokens.is_empty():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="api authentication is not configured: mutating routes are disabled",
        )
    actor = authenticate(request.headers.get("authorization"), tokens)
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.actor = actor
    with log_guard():
        emit(
            logger,
            EventName.LOG_RECORD,
            logging.INFO,
            "api mutation authenticated: %s %s",
            request.method,
            request.url.path,
            attributes={"actor": actor.value, "method": request.method},
        )
    return actor


ApiActor = Annotated[Actor | None, Depends(require_api_actor)]

#: 認証済みの実行者から、手動投稿の承認者への写像（ADR-0080 §B / ADR-0081 §9）。
#: エージェントのトークンは「所有者の指示による代理」= ``AGENT_DELEGATED`` で、REVIEW を解放しない。
_APPROVER_KINDS: dict[Actor, ApproverKind] = {
    Actor.OWNER: ApproverKind.OWNER,
    Actor.AGENT: ApproverKind.AGENT_DELEGATED,
}


def get_upload_approver(actor: ApiActor) -> UploadApprover | None:
    """手動投稿を指示した承認者。認証されていなければ ``None``（= REVIEW を解放しない）。"""
    if actor is None:
        return None
    return UploadApprover(kind=_APPROVER_KINDS[actor], approver_id=f"api-{actor.value}-token")


__all__ = [
    "SAFE_METHODS",
    "TOKEN_MIN_LENGTH",
    "Actor",
    "ApiActor",
    "ApiTokens",
    "authenticate",
    "TokenSource",
    "get_api_token_source",
    "get_upload_approver",
    "load_api_tokens",
    "require_api_actor",
]
