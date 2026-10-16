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
#: 故障注入の発火回数（注入点ごと）を書き出す JSON のパス（INV-38 の検査が読む）
FAULT_REPORT_ENV = "AVP_TEST_FAULT_REPORT"

#: 注入点 → 発火回数。``make_record.<LEVEL>`` はレベル別
FAULTS: dict[str, int] = {}
#: テストファイル（repo 相対）→ 発火回数。suite ごとに注入が経路へ届いたかを見る（レビュー I-12）
FAULTS_BY_FILE: dict[str, int] = {}
#: report の中で ``FAULTS_BY_FILE`` を置くキー（注入点の名前と衝突しない）
BY_FILE_KEY = "_by_file"
_CURRENT_FILE: dict[str, str | None] = {"path": None}


def _fire(point: str) -> None:
    FAULTS[point] = FAULTS.get(point, 0) + 1
    current = _CURRENT_FILE["path"]
    if current is not None:
        FAULTS_BY_FILE[current] = FAULTS_BY_FILE.get(current, 0) + 1
    raise InjectedLoggingFault(f"injected: {point}")


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
    """ログの発行・整形・commit 後の発行・adapter の観測を壊す（INV-38）。

    業務コードの結果・例外の型・台帳の状態は変わってはならない。注入点:

    - ``extra={"avp": ...}`` を持つ記録の生成（``Logger.makeRecord``。emit が握る経路）
    - 整形器（``JsonFormatter.build``。handler が握る経路。``raiseExceptions`` を落とす）
    - commit 後の発行の積み込み（``ledger._queue``。2回に1回）と取り出し
      （``ledger._flush_pending``）
    - adapter の観測（``CallObservation._base``）
    - 予約台帳の行 → フィールド（``reservation_fields``。使う module の名前を差し替える）

    発火回数は ``FAULTS`` に数える（0 なら注入が経路に届いていない）。
    """
    import infrastructure.db.repositories as repositories
    import infrastructure.production.paid_job as paid_job
    from contracts.log_contract import RECORD_EXTRA_KEY
    from infrastructure.logging import ledger
    from infrastructure.logging.formatter import JsonFormatter
    from infrastructure.logging.provider import CallObservation

    original_make = logging.Logger.makeRecord
    original_build = JsonFormatter.build
    original_raise = logging.raiseExceptions
    original_queue = ledger._queue
    original_flush = ledger._flush_pending
    original_base = CallObservation._base
    original_fields = (paid_job.reservation_fields, repositories.reservation_fields)
    queued = {"n": 0}

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        extra = kwargs.get("extra") if "extra" in kwargs else (args[8] if len(args) > 8 else None)
        if isinstance(extra, dict) and RECORD_EXTRA_KEY in extra:
            level = args[1] if len(args) > 1 else kwargs.get("level", 0)
            _fire(f"make_record.{logging.getLevelName(level)}")
        return original_make(self, *args, **kwargs)

    def build(self: JsonFormatter, record: logging.LogRecord) -> dict[str, Any]:
        _fire("formatter.build")
        raise AssertionError("unreachable")  # pragma: no cover

    def queue(sync_session: Any, item: tuple[Any, ...]) -> None:
        queued["n"] += 1
        if queued["n"] % 2:
            _fire("ledger.defer")
        original_queue(sync_session, item)

    def flush(session: Any) -> None:
        if session.info.get(ledger._PENDING_KEY):
            _fire("ledger.after_commit")
        original_flush(session)

    def base(self: CallObservation) -> dict[str, Any]:
        _fire("call_observation")
        raise AssertionError("unreachable")  # pragma: no cover

    def fields(reservation: Any) -> dict[str, Any]:
        _fire("reservation_fields")
        raise AssertionError("unreachable")  # pragma: no cover

    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    JsonFormatter.build = build  # type: ignore[method-assign]
    logging.raiseExceptions = False
    ledger._queue = queue  # type: ignore[assignment]
    ledger._flush_pending = flush  # type: ignore[assignment]
    CallObservation._base = base  # type: ignore[method-assign]
    paid_job.reservation_fields = fields  # type: ignore[assignment]
    repositories.reservation_fields = fields  # type: ignore[assignment]
    try:
        yield
    finally:
        logging.Logger.makeRecord = original_make  # type: ignore[method-assign]
        JsonFormatter.build = original_build  # type: ignore[method-assign]
        logging.raiseExceptions = original_raise
        ledger._queue = original_queue  # type: ignore[assignment]
        ledger._flush_pending = original_flush  # type: ignore[assignment]
        CallObservation._base = original_base  # type: ignore[method-assign]
        paid_job.reservation_fields, repositories.reservation_fields = original_fields  # type: ignore[assignment]


@pytest.fixture
def broken_logging() -> Iterator[None]:
    with break_logging():
        yield


@pytest.fixture(autouse=True)
def _break_logging_everywhere(request: pytest.FixtureRequest) -> Iterator[None]:
    if os.environ.get(BREAK_LOGGING_ENV) != "1":
        yield
        return
    _CURRENT_FILE["path"] = os.path.relpath(request.node.path, request.config.rootpath)
    try:
        with break_logging():
            yield
    finally:
        _CURRENT_FILE["path"] = None


def pytest_configure(config: pytest.Config) -> None:
    from infrastructure.logging.setup import configure_logging

    if os.environ.get(BREAK_LOGGING_ENV) == "1" and os.environ.get(JSON_LOGS_ENV) != "1":
        # 故障注入は本番と同じ経路（DEBUG まで有効・JSON の handler）で走らせる。出力は捨てる
        configure_logging(
            {**os.environ, "AVP_LOG_LEVEL": "DEBUG"},
            open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115 — プロセスの終わりまで使う
            forward_temporal_core=False,
        )
    if os.environ.get(JSON_LOGS_ENV) != "1":
        return

    # capture の外へ（-s を付けなくても stdout に1行1 JSON が出る）
    configure_logging(stream=_REAL_STDOUT or sys.__stdout__)
    install_worker_interceptors()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = os.environ.get(FAULT_REPORT_ENV)
    if path:
        import json

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**FAULTS, BY_FILE_KEY: FAULTS_BY_FILE}, fh, sort_keys=True)
