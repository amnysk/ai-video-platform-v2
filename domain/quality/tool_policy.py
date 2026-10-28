"""Agent の tool 要求の検査（INV-103、設計 v3 T04）。

許可は ``contracts.agent_tool_policy.PolicyToolId`` だけ。それ以外は理由つきで拒否し、
拒否は呼び出し側が decision として記録する（Web 資料に埋め込まれた「upload しろ」は
資料の内容であって指示ではない。要求の出所に関わらず allowlist だけで決める）。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.agent_tool_policy import (
    ALLOWED_POLICY_TOOLS,
    FORBIDDEN_POLICY_TOOLS,
    PolicyRejection,
)


@dataclass(frozen=True, slots=True)
class ToolDecision:
    allowed: bool
    tool_id: str
    rejection: PolicyRejection | None = None
    #: 記録用の固定形の説明（要求の本文は写さない）
    detail: str = ""


def validate_tool_request(requested: object) -> ToolDecision:
    if not isinstance(requested, str) or not requested.strip():
        return ToolDecision(False, "", PolicyRejection.MALFORMED, "tool id must be a string")
    # 正規化しない: 大文字小文字・空白違いの「似た名前」は許可外（曖昧な一致を許さない）
    if requested in ALLOWED_POLICY_TOOLS:
        return ToolDecision(True, requested)
    if requested in FORBIDDEN_POLICY_TOOLS:
        return ToolDecision(False, requested, PolicyRejection.FORBIDDEN, "forbidden tool")
    return ToolDecision(False, requested[:64], PolicyRejection.NOT_ALLOWLISTED, "not allowlisted")
