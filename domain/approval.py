"""承認記録の検証（ADR-0080 / INV-123）。**所有者本人の承認**と**エージェントへの委任**を分ける。

承認の参照は文字列 1 つで、**種別を接頭辞で必ず述べる**（述べない記録は承認として読まない）:

- ``OWNER-<yyyymmdd>-<id>``: 所有者本人が**この操作そのもの**を承認した発言・チケットの参照。
- ``AGENT-<yyyymmdd>-<id>:OWNER-<yyyymmdd>-<id>``: エージェントが、所有者の**条件付きの指示**
  （後ろの ``OWNER-…``）に基づいて自分で承認した。委任元の指示を必ず書く。

認証ではない（誰でも書ける形式検査）。目的は、監査記録から「所有者が見て決めたのか、エージェントが
委任の範囲で決めたのか」を取り違えないこと。所有者専用の関門（:class:`OwnerGate`）は
``AGENT_DELEGATED`` では決して通らない。DB には列を足さず、既存の文字列列（actor / approved_by）に
``<kind>:<name>`` で種別を保存する。種別の無い旧い値は ``None``（= 所有者承認ではない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from contracts.approval import ApproverKind

_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}"
_OWNER_RE = re.compile(rf"OWNER-(?P<date>\d{{8}})-(?P<id>{_ID})")
_AGENT_RE = re.compile(rf"AGENT-(?P<date>\d{{8}})-(?P<id>{_ID}):(?P<owner>OWNER-\d{{8}}-{_ID})")
#: actor 列の長さ（provider_reservation_reconciliations.actor は String(64)）
MAX_ACTOR_LEN = 64 - len("agent_delegated:")


class ApprovalError(ValueError):
    """承認の参照・種別が検証を通らない。"""


class OwnerGate(StrEnum):
    """所有者本人の承認だけが通せる関門（ADR-0080 §3）。追加はあっても緩めない。"""

    ENABLE_NEW_FEATURE = "enable_new_feature"
    RAISE_FAL_BUDGET = "raise_fal_budget"
    PUBLIC_OR_UNLISTED_UPLOAD = "public_or_unlisted_upload"
    RELEASE_PROVIDER_CREDIT_HOLD = "release_provider_credit_hold"
    RESOLVE_RESERVATION_MANUALLY = "resolve_reservation_manually"


OWNER_ONLY_GATES: frozenset[OwnerGate] = frozenset(OwnerGate)


@dataclass(frozen=True, slots=True)
class ApprovalReference:
    kind: ApproverKind
    reference: str
    #: ``AGENT_DELEGATED`` のとき、委任元の所有者の指示の参照
    delegated_by: str | None = None


def _valid_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return False
    return True


def parse_approval_reference(token: str | None) -> ApprovalReference:
    """承認の参照を検証して種別を返す。種別を述べない・形が違う・日付が実在しないなら例外。"""
    if not isinstance(token, str) or token != token.strip() or not token:
        raise ApprovalError("approval reference is empty or has surrounding whitespace")
    owner = _OWNER_RE.fullmatch(token)
    if owner is not None:
        if not _valid_date(owner.group("date")):
            raise ApprovalError("approval reference date is not a real calendar date")
        return ApprovalReference(ApproverKind.OWNER, token)
    agent = _AGENT_RE.fullmatch(token)
    if agent is not None:
        owner_ref = agent.group("owner")
        if not _valid_date(agent.group("date")) or not _valid_date(owner_ref.split("-")[1]):
            raise ApprovalError("approval reference date is not a real calendar date")
        return ApprovalReference(ApproverKind.AGENT_DELEGATED, token, delegated_by=owner_ref)
    raise ApprovalError(
        "approval reference must state its kind: OWNER-<yyyymmdd>-<id> (the owner approved this "
        "action) or AGENT-<yyyymmdd>-<id>:OWNER-<yyyymmdd>-<id> (the agent approved under that "
        "owner instruction). A record that does not state its kind is not an owner approval"
    )


def require_gate_allowed(reference: ApprovalReference | None, gate: OwnerGate) -> None:
    """所有者専用の関門。所有者本人の承認でなければ例外（委任・種別なしは通さない）。"""
    if reference is None or reference.kind is not ApproverKind.OWNER:
        raise ApprovalError(
            f"{gate.value} requires the owner's own approval (OWNER-<yyyymmdd>-<id>); an "
            "agent-delegated or kind-less approval does not satisfy it"
        )


def format_approver(kind: ApproverKind, actor: str) -> str:
    """既存の文字列列に保存する ``<kind>:<name>``。名前に種別を紛れ込ませない。"""
    name = actor.strip()
    if not name or len(name) > MAX_ACTOR_LEN:
        raise ApprovalError(f"approver name must be 1-{MAX_ACTOR_LEN} characters")
    if any(name.startswith(f"{k.value}:") for k in ApproverKind):
        raise ApprovalError("approver name must not start with an approver kind prefix")
    return f"{kind.value}:{name}"


def parse_approver(stored: str | None) -> tuple[ApproverKind | None, str]:
    """保存された値を ``(種別, 名前)`` に戻す。種別の接頭辞が無い旧い値は種別 ``None``。"""
    if not stored:
        return None, ""
    for kind in ApproverKind:
        prefix = f"{kind.value}:"
        if stored.startswith(prefix):
            return kind, stored[len(prefix) :]
    return None, stored


__all__ = [
    "MAX_ACTOR_LEN",
    "OWNER_ONLY_GATES",
    "ApprovalError",
    "ApprovalReference",
    "OwnerGate",
    "format_approver",
    "parse_approval_reference",
    "parse_approver",
    "require_gate_allowed",
]
