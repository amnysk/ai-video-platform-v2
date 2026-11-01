"""承認記録の語彙（ADR-0080）。**唯一の定義**（AGENTS.md §8）。"""

from __future__ import annotations

from enum import StrEnum


class ApproverKind(StrEnum):
    """承認者の種別（ADR-0080）。**唯一の定義**。種別を書かない記録は所有者承認ではない。"""

    OWNER = "owner"
    AGENT_DELEGATED = "agent_delegated"


#: API の認証方式を選ぶ環境変数名。**唯一の定義**（AGENTS.md §8）。
API_AUTH_MODE_ENV = "AVP_API_AUTH_MODE"


class ApiAuthMode(StrEnum):
    """API の変更系ルートの認証方式（ADR-0080 追補 2026-11-01）。**唯一の定義**。

    ``REQUIRED`` が既定。``DISABLED`` は所有者が明示した場合だけ（家庭内 LAN・単独利用）。
    """

    REQUIRED = "required"
    DISABLED = "disabled"


def parse_api_auth_mode(raw: object) -> ApiAuthMode:
    """生の値から方式を決める。**``disabled`` と明示された場合以外は全て REQUIRED**（fail closed）。

    未設定・空・綴り違い・未知の値で認証が外れることはない。
    """
    if isinstance(raw, str) and raw.strip().strip("\"'").lower() == ApiAuthMode.DISABLED.value:
        return ApiAuthMode.DISABLED
    return ApiAuthMode.REQUIRED
