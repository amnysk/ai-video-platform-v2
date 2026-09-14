"""OAuth 2.0 refresh token から access token を得る（installed app, Phase 6）。

- refresh token は **repo 外の 0600 ファイル** から読む（``scripts/youtube-oauth.py`` が書く）
- access token は期限付きでメモリにだけ置く。401 を受けた呼び出し側が ``invalidate`` する
- ``invalid_grant`` 等は人手の再同意が必要 → ``YouTubeAuthError``
- token 類はログ・例外・``repr`` に出さない
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import stat
import time
from collections.abc import Callable

import httpx
from pydantic import SecretStr

from infrastructure.youtube.errors import YouTubeAuthError, YouTubeTransientError

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
#: 期限の少し前に更新する（時計のずれと通信時間の余裕）
EXPIRY_SKEW_SECONDS = 60.0
DEFAULT_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_refresh_token(
    path: str | os.PathLike[str], *, repo_root: pathlib.Path = DEFAULT_REPO_ROOT
) -> SecretStr:
    """refresh token ファイルを検査して読む。値は例外メッセージに出さない。"""
    resolved = pathlib.Path(path).expanduser().resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise YouTubeAuthError("refresh token file must live outside the repository")
    try:
        info = resolved.stat()
    except OSError as exc:
        raise YouTubeAuthError(
            f"refresh token file is not readable ({type(exc).__name__})"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise YouTubeAuthError("refresh token path is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise YouTubeAuthError("refresh token file permissions must be 0600")
    token = resolved.read_text(encoding="utf-8").strip()
    if not token:
        raise YouTubeAuthError("refresh token file is empty")
    return SecretStr(token)


class RefreshTokenCredentials:
    """access token のキャッシュと更新。並行呼び出しでも更新は1回に絞る。"""

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        refresh_token: SecretStr,
        *,
        client: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
        token_endpoint: str = TOKEN_ENDPOINT,
    ) -> None:
        if not client_id:
            raise YouTubeAuthError("youtube client id is not configured")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._client = client
        self._clock = clock
        self._token_endpoint = token_endpoint
        self._access_token: SecretStr | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_file(
        cls,
        client_id: str,
        client_secret: SecretStr,
        refresh_token_path: str | os.PathLike[str],
        *,
        client: httpx.AsyncClient,
        repo_root: pathlib.Path = DEFAULT_REPO_ROOT,
    ) -> RefreshTokenCredentials:
        return cls(
            client_id,
            client_secret,
            load_refresh_token(refresh_token_path, repo_root=repo_root),
            client=client,
        )

    def __repr__(self) -> str:
        return "RefreshTokenCredentials(<redacted>)"

    __str__ = __repr__

    def invalidate(self) -> None:
        """401 を受けたら呼ぶ。次の ``access_token`` で更新する。"""
        self._access_token = None
        self._expires_at = 0.0

    async def access_token(self) -> str:
        async with self._lock:
            if self._access_token is not None and self._clock() < self._expires_at:
                return self._access_token.get_secret_value()
            token, expires_in = await self._refresh()
            self._access_token = SecretStr(token)
            self._expires_at = self._clock() + max(0.0, expires_in - EXPIRY_SKEW_SECONDS)
            return token

    async def _refresh(self) -> tuple[str, float]:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret.get_secret_value(),
            "refresh_token": self._refresh_token.get_secret_value(),
            "grant_type": "refresh_token",
        }
        try:
            response = await self._client.post(self._token_endpoint, data=data)
        except httpx.TransportError as exc:
            raise YouTubeTransientError(
                f"oauth token refresh transport failure ({type(exc).__name__})"
            ) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise YouTubeTransientError(f"oauth token refresh failed: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200:
            code = body.get("error") if isinstance(body, dict) else None
            label = code if isinstance(code, str) and code.isidentifier() else "unknown"
            logger.warning("youtube oauth refresh rejected: %s", label)
            raise YouTubeAuthError(
                f"oauth token refresh rejected: HTTP {response.status_code} {label}"
            )
        token = body.get("access_token") if isinstance(body, dict) else None
        expires_in = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise YouTubeTransientError("oauth token response has no access_token")
        if not isinstance(expires_in, int | float) or isinstance(expires_in, bool):
            expires_in = 0.0
        return token, float(expires_in)
