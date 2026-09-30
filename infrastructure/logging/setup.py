"""プロセスのログ初期化（log-contract §1・§6・§7.6 / ADR-0040 §1）。

``configure_logging()`` を呼んだ時だけ handler を登録する（**import 時の副作用なし**。Workflow の
sandbox が再 import しても handler は増えない）。呼ぶのは起動点（``worker_entry.cli``・
``apps.api.serve``）だけ。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Mapping
from types import TracebackType
from typing import IO

from contracts.log_contract import ENV_LOG_FORMAT, ENV_LOG_LEVEL, LogLevel
from infrastructure.logging.formatter import JsonFormatter, SafeTextFormatter, ServiceIdentity
from infrastructure.logging.redaction import allowed_hosts

#: 未捕捉例外を記録する logger
UNCAUGHT_LOGGER = "avp.uncaught"
#: Temporal Core（Rust）のログの転送先
CORE_LOGGER = "temporalio.core"

#: 第三者 logger の既定レベル（log-contract §6）。httpx/httpcore は URL の query を INFO で出すので
#: WARNING（既存 INV-20 の対策を一か所へ寄せる）。
THIRD_PARTY_LEVELS: dict[str, int] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "temporalio": logging.INFO,
}

_HANDLER_NAME = "avp-stdout"


def _level(env: Mapping[str, str]) -> int:
    raw = (env.get(ENV_LOG_LEVEL) or "").strip().upper()
    return getattr(logging, raw) if raw in {lv.value for lv in LogLevel} else logging.INFO


def _log_uncaught(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger(UNCAUGHT_LOGGER).critical("uncaught exception", exc_info=(exc_type, exc, tb))


def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None or issubclass(args.exc_type, SystemExit):
        return
    thread = args.thread.name if args.thread is not None else "?"
    logging.getLogger(UNCAUGHT_LOGGER).critical(
        "uncaught exception in thread %s",
        thread,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def configure_temporal_loggers() -> None:
    """SDK の logger が message の末尾に info の dict を足さないようにする。

    フィールドは SDK が付ける extra（``temporal_workflow`` / ``temporal_activity``）から取る。
    """
    from temporalio import activity, workflow

    workflow.logger.workflow_info_on_message = False
    activity.logger.activity_info_on_message = False


def install_core_log_forwarding() -> bool:
    """Temporal Core のログを Python logging（``temporalio.core``）へ転送する既定 Runtime を置く。

    最初の connect より前に呼ぶ。既定 Runtime が既にあれば何もしない（``False``）。既定のままだと
    Core は stderr へ直接書き、整形・安全化を通らない（ADR-0040 §1）。
    """
    from temporalio.runtime import (
        LogForwardingConfig,
        LoggingConfig,
        Runtime,
        TelemetryConfig,
        TelemetryFilter,
    )

    runtime = Runtime(
        telemetry=TelemetryConfig(
            logging=LoggingConfig(
                filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                forwarding=LogForwardingConfig(logger=logging.getLogger(CORE_LOGGER)),
            )
        )
    )
    try:
        Runtime.set_default(runtime, error_if_already_set=True)
    except RuntimeError:
        return False
    return True


def configure_logging(
    env: Mapping[str, str] | None = None,
    stream: IO[str] | None = None,
    *,
    forward_temporal_core: bool = True,
) -> logging.Handler:
    """root に stdout の handler を1つだけ置く。何度呼んでも handler は増えない。"""
    env = os.environ if env is None else env
    fmt = (env.get(ENV_LOG_FORMAT) or "json").strip().lower()
    formatter: logging.Formatter = (
        SafeTextFormatter() if fmt == "text" else JsonFormatter(ServiceIdentity.from_env(env))
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_level(env))
    for name, level in THIRD_PARTY_LEVELS.items():
        logging.getLogger(name).setLevel(level)

    # handler の中の失敗を stderr へ出さない・業務へ伝播させない（log-contract §7.9）
    logging.raiseExceptions = False
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught
    configure_temporal_loggers()
    # 許可 host を起動時に一度だけ導く（Workflow スレッドで初めて adapter を import しない）
    allowed_hosts()
    if forward_temporal_core:
        install_core_log_forwarding()
    return handler


def configure_uvicorn_loggers() -> None:
    """uvicorn の logger を root へ流す（独自 handler・stderr を使わない）。"""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


__all__ = [
    "CORE_LOGGER",
    "THIRD_PARTY_LEVELS",
    "UNCAUGHT_LOGGER",
    "configure_logging",
    "configure_temporal_loggers",
    "configure_uvicorn_loggers",
    "install_core_log_forwarding",
]
