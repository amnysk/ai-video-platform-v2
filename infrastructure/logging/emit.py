"""業務イベントの発行ヘルパー（log-contract §5・§9 / INV-38）。

``emit(logger, EventName.X, logging.INFO, "msg", episode_id=..., ...)``。フィールドは
``extra={"avp": {...}}`` の1キーに載せる（LogRecord の予約属性と衝突させない）。

**record の生成を含めて例外を握る**。ログの故障で業務処理の例外・台帳の状態・制御が変わらない。
Workflow のコードはこれを使わない（``workflow.logger`` + ``extra={"avp": {...}}``。INV-40）。
"""

from __future__ import annotations

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


__all__ = ["emit"]
