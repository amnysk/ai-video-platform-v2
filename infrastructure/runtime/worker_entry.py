"""Worker コンテナのエントリポイント。

``python -m infrastructure.runtime.worker_entry workers.render.run_worker``
起動直後（既定 30 秒以内）の失敗は設定不足とみなし、既定 60 秒待ってから非 0 終了する
（``restart: unless-stopped`` の高速再起動ループを防ぐ）。SIGTERM / Ctrl-C は即 0 終了。
ログには例外の型名と SystemExit のメッセージ（秘密を含まない）だけを出す。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from types import FrameType

from contracts.log_contract import EventName, Outcome
from infrastructure.logging.emit import emit, log_guard
from infrastructure.logging.setup import configure_logging

logger = logging.getLogger("worker_entry")

DEFAULT_MIN_UPTIME_SECONDS = 30.0
DEFAULT_FAILURE_BACKOFF_SECONDS = 60.0


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def run(
    module_name: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    env: Mapping[str, str] = os.environ,
) -> int:
    min_uptime = _float_env(env, "AVP_WORKER_MIN_UPTIME_SECONDS", DEFAULT_MIN_UPTIME_SECONDS)
    backoff = _float_env(env, "AVP_WORKER_FAILURE_BACKOFF_SECONDS", DEFAULT_FAILURE_BACKOFF_SECONDS)
    started = clock()
    # どの版のコードかをログから辿れる（イメージの ENV AVP_GIT_REVISION。workers.md §10）
    with log_guard():
        emit(
            logger,
            EventName.SERVICE_STARTED,
            logging.INFO,
            "worker %s starting revision=%s",
            module_name,
            env.get("AVP_GIT_REVISION") or "unknown",
            outcome=Outcome.STARTED.value,
            attributes={"module": module_name},
        )
    try:
        module = importlib.import_module(module_name)
        asyncio.run(module.main())
        _stopped(module_name, "completed")
        return 0
    except KeyboardInterrupt:
        with log_guard():
            emit(
                logger,
                EventName.SERVICE_STOPPED,
                logging.INFO,
                "worker %s interrupted; exiting",
                module_name,
                outcome=Outcome.SUCCEEDED.value,
            )
        return 0
    except SystemExit as exc:
        if exc.code is None or exc.code == 0:
            _stopped(module_name, "exited")
            return 0
        code = exc.code if isinstance(exc.code, int) else 1
        detail = f"SystemExit: {exc.code}"
    except Exception as exc:
        code = 1
        detail = type(exc).__name__
    elapsed = clock() - started
    if elapsed < min_uptime:
        with log_guard():
            emit(
                logger,
                EventName.SERVICE_START_FAILED,
                logging.ERROR,
                "worker %s failed after %.1fs (%s); backing off %.0fs before exit",
                module_name,
                elapsed,
                detail,
                backoff,
                outcome=Outcome.FAILED.value,
                error_type=detail.split(":", 1)[0],
                duration_ms=elapsed * 1000,
            )
        try:
            sleep(backoff)
        except KeyboardInterrupt:
            # 待機中の docker compose stop（SIGTERM）は待たずに正常終了する
            logger.info("worker %s interrupted during backoff; exiting", module_name)
            return 0
    else:
        with log_guard():
            emit(
                logger,
                EventName.SERVICE_STOPPED,
                logging.ERROR,
                "worker %s failed after %.1fs (%s)",
                module_name,
                elapsed,
                detail,
                outcome=Outcome.FAILED.value,
                error_type=detail.split(":", 1)[0],
                duration_ms=elapsed * 1000,
            )
    return code


def _stopped(module_name: str, how: str) -> None:
    with log_guard():
        emit(
            logger,
            EventName.SERVICE_STOPPED,
            logging.INFO,
            "worker %s %s",
            module_name,
            how,
            outcome=Outcome.SUCCEEDED.value,
        )


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    # KeyboardInterrupt を直接投げるとイベントループの任意の地点で割り込み、Temporal Worker の
    # shutdown が戻らない（stop_grace_period 後に SIGKILL される）。SIGINT に転送して
    # asyncio.run の SIGINT 処理（main task の cancel）に任せる。
    # ループ外では KeyboardInterrupt になる
    signal.raise_signal(signal.SIGINT)


def cli(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m infrastructure.runtime.worker_entry <module>", file=sys.stderr)
        return 2
    # 1行1 JSON を stdout へ（ADR-0040）。各 worker の main() は logging を設定しない
    configure_logging()
    signal.signal(signal.SIGTERM, _on_sigterm)
    return run(args[0])


if __name__ == "__main__":
    raise SystemExit(cli())
