"""INV-38: ログが壊れても業務の結果・例外の型・台帳の状態は変わらない。

既存の課金・台帳のテスト群を、ロガーの故障を注入した状態でそのまま（書き換えずに）走らせる。
期待値は既存テストが持っている（台帳の状態・例外の型）ので、全部が通ることがそのまま検査になる。
理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from contracts.log_contract import EventName
from infrastructure.logging import emit
from tests.support.json_log_plugin import (
    BREAK_LOGGING_ENV,
    BY_FILE_KEY,
    FAULT_REPORT_ENV,
    break_logging,
)

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
    # レビュー I-12: YouTube uploader・Render/Research/Pipeline の Activity
    "tests/unit/test_youtube_uploader.py",
    "tests/unit/test_render_activities.py",
    "tests/unit/test_research_workflow.py",
    "tests/unit/test_pipeline_activities.py",
)
#: 記録の**存在**を caplog 等で見るテストを含む suite（レビュー I-12）。故障注入で記録が消えるので
#: そこでは落ちてよいが、落ちてよいのは記録を見る行（``LOG_OBSERVATION_MARKERS`` を含む行）だけ。
#: それより前の業務の検査（``pytest.raises`` の型・台帳の commit）は通っていなければならない
LOG_OBSERVING_SUITES = (
    "tests/unit/test_fal_storage.py",
    "tests/unit/test_log_ledger.py",
)
LOG_OBSERVATION_MARKERS = ("caplog", "logs.")


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
    done, fired = _run_with_broken_logging(tmp_path, LEDGER_SUITES)
    assert done.returncode == 0, done.stdout[-4000:]
    assert " passed" in done.stdout
    missing = [p for p in REQUIRED_FAULT_POINTS if fired.get(p, 0) == 0]
    assert not missing, (missing, fired)
    # suite ごとにも注入が届いていること（レビュー I-12。0 の suite は何も確かめていない）
    by_file = fired[BY_FILE_KEY]
    unreached = [s for s in LEDGER_SUITES if by_file.get(s, 0) == 0]
    assert not unreached, (unreached, by_file)


def _run_with_broken_logging(
    tmp_path: Path, suites: tuple[str, ...], *extra: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
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
            *extra,
            *suites,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return done, json.loads(report.read_text(encoding="utf-8"))


#: ``--tb=line`` の1行（``<path>:<lineno>: <例外の型>: ...``）
_TB_LINE = re.compile(r"^(?P<path>/\S+?\.py):(?P<lineno>\d+): (?P<error>[\w.]+)")


def test_log_observing_suites_fail_only_where_they_read_the_records(tmp_path) -> None:
    """記録を見る suite（fal storage の診断行・commit 後のイベント）も故障注入の下で走らせる。

    記録は消えるので記録を見る assert は落ちる。落ちた位置がテストファイルの記録を見る行で
    あれば、そこまでの業務の処理（例外の型・commit）は注入の下でも変わらなかったことになる。
    業務コードの中で落ちた（``InjectedLoggingFault`` が漏れた・例外の型が変わった）なら、落ちた
    位置は業務コードか ``pytest.raises`` の行になるので、ここで捕まえる。
    """
    done, fired = _run_with_broken_logging(tmp_path, LOG_OBSERVING_SUITES, "--tb=line")
    # 1 = テストの失敗（収集エラー等ではない）
    assert done.returncode in (0, 1), done.stdout[-4000:]
    crashes = [m for line in done.stdout.splitlines() if (m := _TB_LINE.match(line))]
    failed = len([line for line in done.stdout.splitlines() if line.startswith("FAILED ")])
    assert len(crashes) == failed, done.stdout[-4000:]
    bad: list[str] = []
    for crash in crashes:
        path = Path(crash["path"])
        rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
        line = path.read_text(encoding="utf-8").splitlines()[int(crash["lineno"]) - 1]
        if (
            rel not in LOG_OBSERVING_SUITES
            or crash["error"].endswith("InjectedLoggingFault")
            or not any(marker in line for marker in LOG_OBSERVATION_MARKERS)
        ):
            bad.append(f"{rel}:{crash['lineno']} {crash['error']}: {line.strip()}")
    assert not bad, "\n".join(bad)
    by_file = fired[BY_FILE_KEY]
    unreached = [s for s in LOG_OBSERVING_SUITES if by_file.get(s, 0) == 0]
    assert not unreached, (unreached, by_file)
