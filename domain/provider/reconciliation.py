"""未照合予約の照合ロジック（ADR-0053）。純粋関数のみ。I/O をここに書かない。

- ``classify_observation``: provider の観測 → ``ReconciliationVerdict``
- ``advance_verdict``: 判定の進み方。**冪等**で、確定した判定を巻き戻さない
- ``blocks_new_round``: 新しい idempotency key での再送を止める条件
- ``validate_manual_resolution``: 人手の確定（evidence・owner 承認が必須）
- ``summarize_unreconciled``: release preflight 向けの集計

``UNKNOWN`` は失敗ではない。観測できなかったことを理由に失敗・再送へ進めない。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import datetime as _datetime
from decimal import Decimal
from typing import Any, Literal

from contracts.states import (
    RECONCILIATION_TERMINAL_VERDICTS,
    ProviderCall,
    ReconciliationVerdict,
    ReservationStatus,
)

V = ReconciliationVerdict

__all__ = [
    "ManualResolution",
    "ManualResolutionError",
    "ProviderObservation",
    "UnreconciledView",
    "VerdictStep",
    "advance_verdict",
    "blocks_new_round",
    "classify_observation",
    "is_owner_approval_reference",
    "request_id_from_ref",
    "summarize_unreconciled",
    "validate_manual_resolution",
]


class ManualResolutionError(ValueError):
    """人手の確定に必要な項目が足りない・不正。何も書かない。"""


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """provider への**読み取り**で得た観測。secret を含めない。

    ``queue_status`` は provider のキュー状態そのまま（IN_QUEUE / IN_PROGRESS /
    COMPLETED）。``result_outcome`` は結果 body を実際に読めた時だけ入る
    （``ok`` / ``failed``）。読めなかった（403 等）なら ``None``。
    """

    queue_status: str | None = None
    result_outcome: Literal["ok", "failed"] | None = None
    error: str | None = None


def classify_observation(obs: ProviderObservation) -> ReconciliationVerdict:
    if obs.error is not None:
        # 通信・認可エラーは「分からない」。失敗ではない。
        return V.UNKNOWN
    status = obs.queue_status
    if status in ("IN_QUEUE", "IN_PROGRESS"):
        return V.STILL_RUNNING
    if status == "CANCELLED":
        return V.CANCELLED
    if status == "COMPLETED":
        if obs.result_outcome == "ok":
            return V.CONFIRMED_SUCCESS
        if obs.result_outcome == "failed":
            return V.CONFIRMED_FAILED
        # COMPLETED は成功とは限らない。body を読めていなければ成否は不明。
    return V.UNKNOWN


@dataclass(frozen=True, slots=True)
class VerdictStep:
    verdict: ReconciliationVerdict
    changed: bool
    #: 確定済みの判定と食い違う観測があった（通報する。上書きしない）
    conflict: str | None = None


def advance_verdict(
    current: ReconciliationVerdict | None, observed: ReconciliationVerdict
) -> VerdictStep:
    """判定を進める。同じ入力を何度与えても同じ結果（冪等）。

    - 確定（success / failed / cancelled）は他の確定へ変わらない（食い違いは conflict）
    - 確定は running / unknown に戻らない
    - ``UNKNOWN`` の観測は既存の知識を消さない
    """
    if current is None:
        return VerdictStep(observed, True)
    if current == observed:
        return VerdictStep(current, False)
    if current in RECONCILIATION_TERMINAL_VERDICTS:
        if observed in RECONCILIATION_TERMINAL_VERDICTS:
            return VerdictStep(
                current, False, f"terminal verdict {current.value} vs observed {observed.value}"
            )
        return VerdictStep(current, False)
    if observed is V.UNKNOWN:
        return VerdictStep(current, False)
    return VerdictStep(observed, True)


def blocks_new_round(
    status: ReservationStatus,
    verdict: ReconciliationVerdict | None,
    *,
    dispatched: bool = False,
) -> str | None:
    """同じ入力に新しい idempotency key で課金呼び出しを作ってよいか。

    止める理由を返す（``None`` なら止めない）。
    - CONFIRMED_SUCCESS: provider 側に結果がある（課金済み）。取得すべきで再送ではない
    - 未照合（reserved）で UNKNOWN / STILL_RUNNING: 課金中か課金済みかもしれない
    - 照合記録が無い（``verdict is None``）でも、reserved かつ ``dispatched``（呼んだ可能性が
      ある）なら UNKNOWN として扱う。記録が無いことは「無事」の証拠ではない（INV-67）。
      未 dispatch の reserved（呼んでいない証拠）と終端状態（spent / abandoned）は従来どおり。
    """
    if verdict is None and status is ReservationStatus.RESERVED and dispatched:
        verdict = V.UNKNOWN
    if verdict is V.CONFIRMED_SUCCESS:
        return "provider confirmed the job succeeded (billed); fetch its result, do not resubmit"
    if (
        status is ReservationStatus.RESERVED
        and verdict is not None
        and verdict in (V.UNKNOWN, V.STILL_RUNNING)
    ):
        return f"reservation is unreconciled with verdict {verdict.value}; do not resubmit"
    return None


_MANUAL_OUTCOMES: dict[str, ReconciliationVerdict] = {
    "success": V.CONFIRMED_SUCCESS,
    "failure": V.CONFIRMED_FAILED,
    "cancelled": V.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class ManualResolution:
    verdict: ReconciliationVerdict
    evidence: str
    owner_approval_fingerprint: str
    actor: str

    @property
    def reconciled_by(self) -> str:
        return f"operator:{self.actor}"


_OWNER_APPROVAL_RE = re.compile(r"^OWNER-(\d{8})-[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")


def is_owner_approval_reference(token: str) -> bool:
    """``OWNER-<yyyymmdd>-<id>`` の形か。**認証ではない**（誰でも書ける）。

    承認の参照番号（チケット・チャットの発言 id 等）を残させ、監査行から辿れるようにするための
    形式検査。日付は実在する暦日であること。
    """
    match = _OWNER_APPROVAL_RE.match(token)
    if match is None:
        return False
    try:
        _datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError:
        return False
    return True


def validate_manual_resolution(
    *, outcome: str, evidence: str, owner_approval: str, actor: str
) -> ManualResolution:
    """success / failure / cancelled への手動確定の入力検査。

    owner 承認 token は ``OWNER-<yyyymmdd>-<id>`` 形式（認証ではない）。
    保存・ログに出さず、指紋（sha256 の先頭16桁）だけを残す。
    """
    verdict = _MANUAL_OUTCOMES.get(outcome)
    if verdict is None:
        raise ManualResolutionError(f"outcome must be one of {sorted(_MANUAL_OUTCOMES)}")
    if not evidence.strip():
        raise ManualResolutionError("--evidence is required")
    if not owner_approval.strip():
        raise ManualResolutionError("--owner-approval is required")
    if not is_owner_approval_reference(owner_approval.strip()):
        raise ManualResolutionError(
            "--owner-approval must be an owner approval reference like OWNER-<yyyymmdd>-<id> "
            "(valid date, id of 6-64 chars [A-Za-z0-9_-]). This is a format check for "
            "traceability, not authentication"
        )
    if not actor.strip():
        raise ManualResolutionError("--actor is required")
    fingerprint = hashlib.sha256(owner_approval.strip().encode()).hexdigest()[:16]
    return ManualResolution(verdict, evidence.strip(), fingerprint, actor.strip())


def request_id_from_ref(ref: str | None) -> str | None:
    """台帳の provider_job_ref（不透明な JSON）から request id だけを読む。壊れていれば None。"""
    if not ref:
        return None
    try:
        data = json.loads(ref)
    except ValueError:
        return None
    value = data.get("request_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class UnreconciledView:
    reservation_id: str
    episode_id: str
    job_id: str | None
    provider: ProviderCall
    scene_id: str | None
    provider_request_id: str | None
    reserved_at: datetime
    age_seconds: int
    dispatched: bool
    verdict: ReconciliationVerdict
    estimated_cost_usd: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "episode_id": self.episode_id,
            "job_id": self.job_id,
            "provider": self.provider.value,
            "scene_id": self.scene_id,
            "provider_request_id": self.provider_request_id,
            "reserved_at": self.reserved_at.isoformat(),
            "age_seconds": self.age_seconds,
            "dispatched": self.dispatched,
            "verdict": self.verdict.value,
            "estimated_cost_usd": (
                None if self.estimated_cost_usd is None else str(self.estimated_cost_usd)
            ),
        }


def summarize_unreconciled(views: Sequence[UnreconciledView]) -> dict[str, Any]:
    """release preflight の risk-based 判定向け集計（読み取り専用データ）。"""
    return {
        "count": len(views),
        "by_provider": dict(Counter(v.provider.value for v in views)),
        "by_episode": dict(Counter(v.episode_id for v in views)),
        "by_verdict": dict(Counter(v.verdict.value for v in views)),
        "oldest_age_seconds": max((v.age_seconds for v in views), default=0),
        "items": [v.as_dict() for v in views],
    }
