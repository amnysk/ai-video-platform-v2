"""INV-38: ログが壊れても業務の結果・例外の型・台帳の状態は変わらない。

既存の課金・台帳のテスト群を、ロガーの故障を注入した状態でそのまま（書き換えずに）走らせる。
期待値は既存テストが持っている（台帳の状態・例外の型）ので、全部が通ることがそのまま検査になる。
理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from contracts.log_contract import EventName
from infrastructure.logging import emit
from tests.support.json_log_plugin import BREAK_LOGGING_ENV, break_logging

REPO = Path(__file__).resolve().parents[2]
#: 予約台帳・有料 submit/await・provider adapter・Upload の台帳。caplog で文言を見るテストを
#: 含まない（故障注入で記録そのものが消えるため）
LEDGER_SUITES = (
    "tests/unit/test_paid_job.py",
    "tests/unit/test_fal_queue.py",
    "tests/unit/test_provider_reservations.py",
)


def test_the_fault_injection_really_breaks_emission() -> None:
    """注入が効いていること（効いていなければ下の検査は何も確かめていない）。"""
    seen: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    logger = logging.getLogger("tests.fault")
    logger.addHandler(Keep())
    logger.setLevel(logging.INFO)
    with break_logging():
        emit(logger, EventName.RESERVATION_RESERVED, logging.INFO, "reserved")
    assert seen == []
    emit(logger, EventName.RESERVATION_RESERVED, logging.INFO, "reserved")
    assert len(seen) == 1


def test_ledger_suites_pass_unchanged_with_broken_logging() -> None:
    env = {**os.environ, BREAK_LOGGING_ENV: "1"}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.support.json_log_plugin",
            "-p",
            "no:cacheprovider",
            *LEDGER_SUITES,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:]
    assert " passed" in done.stdout
