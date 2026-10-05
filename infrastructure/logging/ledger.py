"""commit の後にだけ出すイベント（log-contract §9）。

予約台帳・成果物・認可障害・拒否・日次枠の変更は、その変更を含む **commit が成功した後**にだけ
発行する。書き込む repository のメソッドが ``defer(session, ...)`` でイベントを session に積み、
SQLAlchemy の ``after_commit`` で発行する。rollback・commit の失敗・commit せずに閉じた
transaction では捨てる（発行しない）。

- 呼び出し側（Activity・``PaidJobRunner``・upload）の書き込み順序・commit の位置・例外は変えない
- 台帳の書き手は複数（paid_job・upload・planning・storyboard・scene_recovery）あるが、
  repository の1か所で拾うので書き手ごとに発行を足さない
- listener は最初の ``defer`` の時に1度だけ登録する（import 時の副作用なし）
- 発行の失敗は握る（INV-38）
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from contracts.log_contract import EventName
from infrastructure.logging.emit import emit

_PENDING_KEY = "avp_log_pending"
_lock = threading.Lock()
_installed = {"done": False}


def _sync_session(session: Any) -> Any:
    return getattr(session, "sync_session", session)


def _after_commit(session: Any) -> None:
    try:
        pending = session.info.pop(_PENDING_KEY, None) or []
    except Exception:
        return
    for logger_name, event, level, msg, args, fields in pending:
        emit(logging.getLogger(logger_name), event, level, msg, *args, **fields)


def _after_transaction_end(session: Any, transaction: Any) -> None:
    # commit 済みの分は after_commit が既に取り出している。残りは rollback・close の分なので捨てる
    try:
        if getattr(transaction, "parent", None) is None and not getattr(
            transaction, "nested", False
        ):
            session.info.pop(_PENDING_KEY, None)
    except Exception:
        pass


def _install() -> None:
    if _installed["done"]:
        return
    with _lock:
        if _installed["done"]:
            return
        from sqlalchemy import event
        from sqlalchemy.orm import Session

        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_transaction_end", _after_transaction_end)
        _installed["done"] = True


def defer(
    session: Any,
    logger_name: str,
    event: EventName,
    level: int,
    msg: str,
    *args: object,
    **fields: Any,
) -> None:
    """``session``（AsyncSession でも Session でも）の commit が成功したら発行する。"""
    try:
        _install()
        sync = _sync_session(session)
        sync.info.setdefault(_PENDING_KEY, []).append(
            (logger_name, event, level, msg, args, fields)
        )
    except Exception:  # ログの故障は業務へ伝播させない（INV-38）
        pass


def reservation_fields(reservation: Any) -> dict[str, Any]:
    """予約台帳の行（``ProviderReservation``）→ ログのフィールド。"""
    status = getattr(reservation, "status", None)
    provider = getattr(reservation, "provider", None)
    return {
        "reservation_id": getattr(reservation, "id", None),
        "reservation_status": getattr(status, "value", status),
        "provider": getattr(provider, "value", provider),
        "provider_attempt": getattr(reservation, "round", None),
        "input_hash": getattr(reservation, "input_hash", None),
        "episode_id": getattr(reservation, "episode_id", None),
        "scene_id": getattr(reservation, "scene_id", None),
        "job_id": getattr(reservation, "job_id", None),
    }


__all__ = ["defer", "reservation_fields"]
