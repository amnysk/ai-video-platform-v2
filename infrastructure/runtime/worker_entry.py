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
    logger.info(
        "worker %s starting revision=%s", module_name, env.get("AVP_GIT_REVISION") or "unknown"
    )
    try:
        module = importlib.import_module(module_name)
        asyncio.run(module.main())
        return 0
    except KeyboardInterrupt:
        logger.info("worker %s interrupted; exiting", module_name)
        return 0
    except SystemExit as exc:
        if exc.code is None or exc.code == 0:
            return 0
        code = exc.code if isinstance(exc.code, int) else 1
        detail = f"SystemExit: {exc.code}"
    except Exception as exc:
        code = 1
        detail = type(exc).__name__
    elapsed = clock() - started
    if elapsed < min_uptime:
        logger.error(
            "worker %s failed after %.1fs (%s); backing off %.0fs before exit",
            module_name,
            elapsed,
            detail,
            backoff,
        )
        try:
            sleep(backoff)
        except KeyboardInterrupt:
            # 待機中の docker compose stop（SIGTERM）は待たずに正常終了する
            logger.info("worker %s interrupted during backoff; exiting", module_name)
            return 0
    else:
        logger.error("worker %s failed after %.1fs (%s)", module_name, elapsed, detail)
    return code


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
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _on_sigterm)
    return run(args[0])


if __name__ == "__main__":
    raise SystemExit(cli())
