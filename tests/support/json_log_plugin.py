"""既存のテストを、本番と同じ JSON ログ・Activity interceptor つきで走らせる pytest plugin。

使い方（docs/observability/emission-points.md §テストで JSON ログを出す）:

    AVP_TEST_JSON_LOGS=1 AVP_SERVICE_NAME=pytest AVP_ENVIRONMENT=test \\
        pytest -p tests.support.json_log_plugin tests/integration/test_incident_recovery_e2e.py

- ``AVP_TEST_JSON_LOGS=1``: ``configure_logging()`` を **pytest の capture の外**
  （capture 前に複製した fd 1）
  に向けて呼び、テストが自前で組む ``temporalio.worker.Worker(...)`` にも
  ``worker_interceptors()`` を足す（既存テストを書き換えない）。``AVP_LOG_FORMAT=text`` も効く。
- ``AVP_TEST_BREAK_LOGGING=1``: 全テストでロガーの故障を注入する（INV-38 の検査）。
- fixture ``broken_logging``: 1つのテストだけで故障を注入する。

テストの既定の実行では読み込まれない（``-p`` で明示した時だけ）。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

JSON_LOGS_ENV = "AVP_TEST_JSON_LOGS"
BREAK_LOGGING_ENV = "AVP_TEST_BREAK_LOGGING"

#: pytest の fd capture が始まる前（``-p`` でこの module が import される時点）の stdout を複製して
#: おく。capture の後では fd 1 自体が一時ファイルへ差し替わっていて ``sys.__stdout__`` でも届かない
_REAL_STDOUT = (
    os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    if os.environ.get(JSON_LOGS_ENV) == "1"
    else None
)


class InjectedLoggingFault(RuntimeError):
    """注入したロガーの故障。業務の例外に混ざったらテストが落ちる（型で見分ける）。"""


def install_worker_interceptors() -> None:
    """``temporalio.worker.Worker`` の生成に ``worker_interceptors()`` を足す（重複させない）。"""
    from temporalio.worker import Worker

    from infrastructure.logging.temporal import ActivityLoggingInterceptor, worker_interceptors

    original = Worker.__init__
    if getattr(original, "_avp_wrapped", False):
        return

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        current = list(kwargs.get("interceptors") or ())
        if not any(isinstance(i, ActivityLoggingInterceptor) for i in current):
            kwargs["interceptors"] = [*current, *worker_interceptors()]
        original(self, *args, **kwargs)

    __init__._avp_wrapped = True  # type: ignore[attr-defined]
    Worker.__init__ = __init__  # type: ignore[method-assign]


@contextmanager
def break_logging() -> Iterator[None]:
    """ログの発行と整形を壊す。業務コードの結果・例外・台帳は変わってはならない（INV-38）。

    - ``extra={"avp": ...}`` を持つ記録（業務イベント）の生成で例外を投げる（emit が握る経路）
    - 整形器そのものも例外を投げる（handler が握る経路。``raiseExceptions`` を落とす）
    """
    from contracts.log_contract import RECORD_EXTRA_KEY
    from infrastructure.logging.formatter import JsonFormatter

    original_make = logging.Logger.makeRecord
    original_build = JsonFormatter.build
    original_raise = logging.raiseExceptions

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        extra = kwargs.get("extra") if "extra" in kwargs else (args[8] if len(args) > 8 else None)
        if isinstance(extra, dict) and RECORD_EXTRA_KEY in extra:
            raise InjectedLoggingFault("injected: record creation failed")
        return original_make(self, *args, **kwargs)

    def build(self: JsonFormatter, record: logging.LogRecord) -> dict[str, Any]:
        raise InjectedLoggingFault("injected: formatting failed")

    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    JsonFormatter.build = build  # type: ignore[method-assign]
    logging.raiseExceptions = False
    try:
        yield
    finally:
        logging.Logger.makeRecord = original_make  # type: ignore[method-assign]
        JsonFormatter.build = original_build  # type: ignore[method-assign]
        logging.raiseExceptions = original_raise


@pytest.fixture
def broken_logging() -> Iterator[None]:
    with break_logging():
        yield


@pytest.fixture(autouse=True)
def _break_logging_everywhere() -> Iterator[None]:
    if os.environ.get(BREAK_LOGGING_ENV) != "1":
        yield
        return
    with break_logging():
        yield


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get(JSON_LOGS_ENV) != "1":
        return
    from infrastructure.logging.setup import configure_logging

    # capture の外へ（-s を付けなくても stdout に1行1 JSON が出る）
    configure_logging(stream=_REAL_STDOUT or sys.__stdout__)
    install_worker_interceptors()
