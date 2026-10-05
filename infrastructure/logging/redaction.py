"""stdout に出す前の安全化（log-contract §7 / INV-39）。

振る舞いだけを持つ。語彙と上限は ``contracts.log_contract``。**置換してから切り詰める**
（切り詰めで秘密のパターンが途中で切れて検出を逃れないように）。

どの関数も同じ入力に二度かけても結果が変わらない（既存の adapter の伏せ字処理の出力に重ねて
かけても壊れない。ADR-0040 §3）。
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from contracts.log_contract import REDACTED

#: キー名（小文字化・``-``→``_``）がこれを**含む**なら値を伏せる（log-contract §7.2）
SECRET_KEY_PARTS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "dsn",
    "database_url",
    "connection_string",
    "signature",
    "x_amz_",
    "fal_key",
    "session_uri",
    "upload_url",
    "access_key",
)
#: キー名がこれに**一致する**なら値を伏せる
SECRET_KEY_EXACT: frozenset[str] = frozenset(
    {"key", "code", "location", "x_goog_upload_url", "upload_id"}
)

#: 縮約した URL の印（``scheme://host/…#sha256:<12桁>``）。二度目の安全化で再縮約しない
_SHRUNK_PATH = "/…"
_SHRUNK_FRAGMENT_RE = re.compile(r"^sha256:[0-9a-f]{12}$")

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
#: SQLAlchemy の例外文の ``[parameters: …]``（行の値。prompt や token を含み得る）
_SQL_PARAMETERS_RE = re.compile(
    r"\[parameters: .*?\](?=\s*(?:\n|\(Background on this error|$))", re.DOTALL
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic|Key)\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=:-]{8,}")
#: fal の API key（``<uuid>:<32 hex>``）
_FAL_KEY_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:[0-9a-fA-F]{32}\b"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_GOOGLE_TOKEN_RE = re.compile(r"(?:\bya29\.[A-Za-z0-9._-]+|(?<![\w/])1//[A-Za-z0-9._-]{10,})")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
#: 文中の ``api_key=…`` / ``"access_token": "…"`` の形（値だけ伏せ、キー名は残す）
_KEY_VALUE_RE = re.compile(
    r"(?i)(\b[A-Za-z0-9_-]*(?:api[_-]?key|apikey|token|secret|password|passwd|credential|"
    r"fal[_-]?key|private[_-]?key)[A-Za-z0-9_-]*[\"']?\s*[:=]\s*[\"']?)"
    r"(?!\[REDACTED\])([^\s\"',&;)\]}]+)"
)
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{1,30}://[^\s\"'<>`]+")
#: 長い base64 様の値（メディアのバイト列・data URL の本体）
_LONG_BLOB_RE = re.compile(r"[A-Za-z0-9+/=_-]{256,}")


@functools.cache
def allowed_hosts() -> frozenset[str]:
    """path を残してよい API host。**adapter の定数から導く**（log-contract §7.4）。

    写しを持たない: adapter が endpoint を変えたら、ここは自動で追従する。import は遅延させる
    （adapter はこのパッケージを import するので、module 読み込み時に辿ると循環する）。
    ``configure_logging()`` が起動時に一度呼んで温める（Workflow スレッドで初めて import しない）。
    """
    from infrastructure.analytics.youtube_analytics import REPORTS_ENDPOINT
    from infrastructure.providers.fal_queue import QUEUE_BASE_URL
    from infrastructure.providers.fal_storage import STORAGE_TOKEN_URL
    from infrastructure.youtube.oauth import TOKEN_ENDPOINT, TOKENINFO_ENDPOINT
    from infrastructure.youtube.uploader import API_BASE_URL, UPLOAD_URL

    urls = (
        QUEUE_BASE_URL,
        STORAGE_TOKEN_URL,
        UPLOAD_URL,
        API_BASE_URL,
        TOKEN_ENDPOINT,
        TOKENINFO_ENDPOINT,
        REPORTS_ENDPOINT,
    )
    return frozenset((urlsplit(u).hostname or "").lower() for u in urls) - {""}


def is_secret_key(key: str) -> bool:
    norm = key.strip().lower().replace("-", "_")
    return norm in SECRET_KEY_EXACT or any(part in norm for part in SECRET_KEY_PARTS)


def _shrink(url: str, parts: Any) -> str:
    digest = hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:12]
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{_SHRUNK_PATH}#sha256:{digest}"


def sanitize_url(url: str) -> str:
    """userinfo・query・fragment を落とし、許可 host 以外は path も縮約する。"""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return REDACTED
    scheme = parts.scheme.lower()
    if parts.path == _SHRUNK_PATH and _SHRUNK_FRAGMENT_RE.match(parts.fragment or ""):
        return url  # 縮約済み
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if "@" in parts.netloc:
        netloc = f"{REDACTED}@{netloc}"
    if scheme not in ("http", "https"):
        # DSN 等。userinfo だけ伏せ、query（sslpassword 等を含み得る）は落とす
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    if host in allowed_hosts():
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return _shrink(url, parts)


def _sub_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    # 文末の句読点・閉じ括弧は URL に含めない
    trail = ""
    while raw and raw[-1] in ".,;:!?)]}":
        trail = raw[-1] + trail
        raw = raw[:-1]
    return sanitize_url(raw) + trail


def sanitize_text(text: str) -> tuple[str, bool]:
    """秘密のパターン・URL を置換する。``(安全化後, 変えたか)``。"""
    if not text:
        return text, False
    out = _PRIVATE_KEY_RE.sub(REDACTED, text)
    out = _SQL_PARAMETERS_RE.sub(f"[parameters: {REDACTED}]", out)
    out = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _FAL_KEY_RE.sub(REDACTED, out)
    out = _JWT_RE.sub(REDACTED, out)
    out = _GOOGLE_TOKEN_RE.sub(REDACTED, out)
    out = _OPENAI_KEY_RE.sub(REDACTED, out)
    out = _URL_RE.sub(_sub_url, out)
    out = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _LONG_BLOB_RE.sub(REDACTED, out)
    return out, out != text


class Redactor:
    """1イベント分の安全化。置換・縮約をしたかを ``applied`` に貯める。"""

    def __init__(self) -> None:
        self.applied = False

    def text(self, value: str) -> str:
        out, changed = sanitize_text(value)
        self.applied = self.applied or changed
        return out

    def value(self, value: Any, *, key: str | None = None, depth: int = 0) -> Any:
        """JSON にできる形へ写しながら安全化する。秘密を示すキーの値は丸ごと伏せる。"""
        if key is not None and is_secret_key(key) and value not in (None, "", REDACTED):
            self.applied = True
            return REDACTED
        if depth > 6:
            return self.text(repr(value)[:200])
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bytes | bytearray | memoryview):
            # メディアのバイト列は出さない。大きさだけ
            return f"<{len(value)} bytes>"
        if isinstance(value, Mapping):
            return {
                str(k): self.value(v, key=str(k), depth=depth + 1)
                for k, v in list(value.items())[:100]
            }
        if isinstance(value, list | tuple | set | frozenset):
            return [self.value(v, depth=depth + 1) for v in list(value)[:100]]
        return self.text(str(value))


__all__ = [
    "SECRET_KEY_EXACT",
    "SECRET_KEY_PARTS",
    "Redactor",
    "allowed_hosts",
    "is_secret_key",
    "sanitize_text",
    "sanitize_url",
]
