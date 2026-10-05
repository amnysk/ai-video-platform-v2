"""INV-38: ログが壊れても業務の結果・例外の型・台帳の状態は変わらない。

既存の課金・台帳のテスト群を、ロガーの故障を注入した状態でそのまま（書き換えずに）走らせる。
期待値は既存テストが持っている（台帳の状態・例外の型）ので、全部が通ることがそのまま検査になる。
理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from contracts.log_contract import EventName
from infrastructure.logging import emit
from tests.support.json_log_plugin import BREAK_LOGGING_ENV, FAULT_REPORT_ENV, break_logging

REPO = Path(__file__).resolve().parents[2]
#: 予約台帳・有料 submit/await・provider adapter・画像/動画/代替案/Upload の Activity。
#: caplog で記録の**存在**を見るテストを含まない（故障注入で記録そのものが消えるため）
LEDGER_SUITES = (
    "tests/unit/test_paid_job.py",
    "tests/unit/test_fal_queue.py",
    "tests/unit/test_provider_reservations.py",
    "tests/unit/test_production_image_activities.py",
    "tests/unit/test_production_video_activities.py",
    "tests/unit/test_scene_alternative_activity.py",
    "tests/unit/test_scene_identity_reuse.py",
    "tests/unit/test_upload_activities.py",
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


#: 注入点ごとに「実際に発火した」回数の下限（0 なら注入が業務の経路に届いていない）
REQUIRED_FAULT_POINTS = (
    "make_record.INFO",
    "make_record.WARNING",
    "formatter.build",
    "ledger.defer",
    "ledger.after_commit",
    "call_observation",
    "reservation_fields",
)


def test_ledger_suites_pass_unchanged_with_broken_logging(tmp_path) -> None:
    """注入は INFO の発行・整形器・commit 後の発行・adapter の観測まで届いていなければ意味がない。

    レビュー I-1: 以前は logger が INFO 無効のままで、emit() がレベル判定で先に return し、
    壊した makeRecord に1度も届いていなかった。ここでは root を DEBUG・JSON handler つきにして
    走らせ、注入点ごとの発火回数を数えて 0 でないことを確かめる。
    """
    report = tmp_path / "faults.json"
    env = {**os.environ, BREAK_LOGGING_ENV: "1", FAULT_REPORT_ENV: str(report)}
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
    fired = json.loads(report.read_text(encoding="utf-8"))
    missing = [p for p in REQUIRED_FAULT_POINTS if fired.get(p, 0) == 0]
    assert not missing, (missing, fired)
