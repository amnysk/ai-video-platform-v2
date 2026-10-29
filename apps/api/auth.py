"""API の変更系ルートの認証（ADR-0080 §B / INV-124）。

- 変更系（POST / PUT / PATCH / DELETE）は ``Authorization: Bearer <token>`` を要求する。
  読み取り（GET 等）は開けたまま（運用者・監視・healthcheck が使い、データの変更・課金・投稿を
  起こさない）。
- トークンは repo 外の 0600 のファイル（``Settings.api_tokens_file`` = ``AVP_API_TOKENS_FILE``）から
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
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from apps.api.dependencies import get_settings
from contracts.log_contract import EventName
from infrastructure.config import Settings
from infrastructure.logging.emit import emit, log_guard

logger = logging.getLogger("avp.api.auth")

TOKEN_MIN_LENGTH = 32
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


def load_api_tokens(path: str | None) -> ApiTokens:
    """token ファイルを読む。どんな不備でも例外にせず、空（= 未設定 = 全拒否）を返す。"""
    if not path:
        return ApiTokens()
    try:
        info = os.stat(path)
    except OSError:
        return _refuse("not readable")
    if not stat.S_ISREG(info.st_mode):
        return _refuse("not a regular file")
    if info.st_mode & 0o077:
        return _refuse("readable by group/others (must be 0600)")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return _refuse("not readable")
    pairs: list[tuple[Actor, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, token = line.partition(":")
        try:
            actor = Actor(name.strip())
        except ValueError:
            return _refuse("unknown actor")
        token = token.strip()
        if not sep or len(token) < TOKEN_MIN_LENGTH:
            return _refuse("malformed or too short token")
        pairs.append((actor, token))
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


def get_api_tokens(settings: Annotated[Settings, Depends(get_settings)]) -> ApiTokens:
    """リクエストごとにファイルを読む（鍵の差し替えが再起動なしで効く。小さなファイル）。"""
    return load_api_tokens(settings.api_tokens_file)


async def require_api_actor(
    request: Request, tokens: Annotated[ApiTokens, Depends(get_api_tokens)]
) -> Actor | None:
    """変更系ルートの認証。読み取りは通す（None）。未設定は 503、不正は 401。"""
    if request.method in SAFE_METHODS:
        return None
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

__all__ = [
    "SAFE_METHODS",
    "TOKEN_MIN_LENGTH",
    "Actor",
    "ApiActor",
    "ApiTokens",
    "authenticate",
    "get_api_tokens",
    "load_api_tokens",
    "require_api_actor",
]
