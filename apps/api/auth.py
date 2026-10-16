"""介入 API の principal（JP-MAS-001 / ADR-0041 §8、INV-47）。

設定 ``INTERVENTION_PRINCIPALS`` は **token の sha256（小文字 hex）→ principal** の JSON か、
その JSON ファイルのパス。平文の token は設定にも DB にもログにも置かない。
照合は受け取った Bearer token の sha256 を引くだけ。

読み込み時に拒否するもの（読み込めない設定 = 介入 API は 503。API 全体は止めない）:

- hash でない key（平文 token を key に置いた誤設定を含む）
- 未知の scope・未知の ``actor_type``・``system``（内部の主体。API の principal にしない）
- AI（``ai_assistant`` / ``agent``）への人専用 scope（``contracts.intervention.HUMAN_ONLY_SCOPES``）

例外の文言には key（hash）も token も入れない（設定の誤りはログに出るため）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.intervention import HUMAN_ONLY_SCOPES, ActorType, Scope

#: API の principal になれる主体。``system`` は内部（worker・Schedule）だけ
PRINCIPAL_ACTOR_TYPES: frozenset[ActorType] = frozenset(
    {ActorType.HUMAN, ActorType.AI_ASSISTANT, ActorType.AGENT}
)
#: 人専用 scope を持てない主体
AI_ACTOR_TYPES: frozenset[ActorType] = frozenset({ActorType.AI_ASSISTANT, ActorType.AGENT})

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class PrincipalConfigError(ValueError):
    """``INTERVENTION_PRINCIPALS`` を読み込めない。文言に secret・hash を含めない。"""


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: 認証できた主体の観測先。ログ用 middleware が要求ごとに空の dict を置き（ADR-0042 §6）、
#: ``authenticate`` が成功したときに actor_type / actor_id だけを書く。token・hash は入れない。
AUTH_OBSERVER: ContextVar[dict[str, str] | None] = ContextVar("avp_auth_observer", default=None)


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    actor_type: ActorType
    actor_id: str
    scopes: frozenset[Scope]

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128)
    scopes: tuple[Scope, ...]


class PrincipalRegistry:
    def __init__(self, by_hash: Mapping[str, ApiPrincipal]) -> None:
        self._by_hash = dict(by_hash)

    def __len__(self) -> int:
        return len(self._by_hash)

    def __repr__(self) -> str:  # hash を表示しない
        return f"PrincipalRegistry(<{len(self._by_hash)} principals>)"

    def authenticate(self, token: str) -> ApiPrincipal | None:
        if not token:
            return None
        digest = token_sha256(token)
        found: ApiPrincipal | None = None
        for key, principal in self._by_hash.items():
            # 全件を定数時間で比べる（一致位置で時間が変わらない）
            if hmac.compare_digest(key, digest):
                found = principal
        observer = AUTH_OBSERVER.get()
        if found is not None and observer is not None:
            observer["actor_type"] = found.actor_type.value
            observer["actor_id"] = found.actor_id
        return found


def _principal(position: int, value: Any) -> ApiPrincipal:
    try:
        entry = _Entry.model_validate(value)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "-" for err in exc.errors()})
        raise PrincipalConfigError(
            f"principal #{position}: invalid entry (fields: {', '.join(fields)})"
        ) from None
    if entry.actor_type not in PRINCIPAL_ACTOR_TYPES:
        raise PrincipalConfigError(
            f"principal #{position} ({entry.actor_id}): actor_type {entry.actor_type.value!r} "
            "cannot be an API principal"
        )
    scopes = frozenset(entry.scopes)
    if entry.actor_type in AI_ACTOR_TYPES:
        forbidden = sorted(s.value for s in scopes & HUMAN_ONLY_SCOPES)
        if forbidden:
            raise PrincipalConfigError(
                f"principal #{position} ({entry.actor_id}): human-only scopes {forbidden} "
                f"cannot be granted to actor_type {entry.actor_type.value!r} (INV-47)"
            )
    return ApiPrincipal(actor_type=entry.actor_type, actor_id=entry.actor_id, scopes=scopes)


def parse_principals(raw: str) -> PrincipalRegistry:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise PrincipalConfigError("INTERVENTION_PRINCIPALS is not valid JSON") from None
    if not isinstance(data, dict):
        raise PrincipalConfigError("INTERVENTION_PRINCIPALS must be a JSON object")
    by_hash: dict[str, ApiPrincipal] = {}
    for position, (key, value) in enumerate(data.items(), start=1):
        if not isinstance(key, str) or not _SHA256_HEX.match(key):
            raise PrincipalConfigError(
                f"principal #{position}: key must be the lowercase hex sha256 of the token "
                "(never the token itself)"
            )
        by_hash[key] = _principal(position, value)
    return PrincipalRegistry(by_hash)


def load_principals(value: str | None) -> PrincipalRegistry | None:
    """設定値を読む。未設定（空）なら ``None`` = 介入 API は無効。

    ``{`` で始まれば JSON、それ以外はファイルのパスとして読む。
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not text.startswith("{"):
        path = Path(text)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise PrincipalConfigError(
                "INTERVENTION_PRINCIPALS file cannot be read (check the path and permissions)"
            ) from None
    return parse_principals(text)
