"""業務イベントの発行ヘルパー（log-contract §5・§9 / INV-38）。

``emit(logger, EventName.X, logging.INFO, "msg", episode_id=..., ...)``。フィールドは
``extra={"avp": {...}}`` の1キーに載せる（LogRecord の予約属性と衝突させない）。

**record の生成を含めて例外を握る**。ログの故障で業務処理の例外・台帳の状態・制御が変わらない。
Workflow のコードはこれを使わない（``workflow.logger`` + ``extra={"avp": {...}}``。INV-40）。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from contracts.log_contract import RECORD_EXTRA_KEY, EventName


def emit(
    logger: logging.Logger | logging.LoggerAdapter[Any],
    event: EventName,
    level: int,
    msg: str,
    *args: object,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    """``None`` のフィールドは付けない（未取得の値を推測で埋めない）。"""
    try:
        if not logger.isEnabledFor(level):
            return
        payload = {k: v for k, v in fields.items() if v is not None}
        payload["event_name"] = event.value
        logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            extra={RECORD_EXTRA_KEY: payload},
            stacklevel=2,
        )
    except Exception:  # ログの故障は業務へ伝播させない（INV-38）
        pass


def log_guard() -> contextlib.AbstractContextManager[None]:
    """発行の**引数の組み立てごと**握る block（``with log_guard(): emit(...)``）。

    ``emit()`` の try は呼ばれた後の失敗しか握れない。引数の計算（分類・行→フィールド・
    ``str(exc)`` 等）が投げると、except 節の中では業務の例外が置き換わる（INV-38 / レビュー I-2）。
    """
    return contextlib.suppress(Exception)


__all__ = ["emit", "log_guard"]
