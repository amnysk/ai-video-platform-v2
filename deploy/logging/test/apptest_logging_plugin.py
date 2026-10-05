"""pytest plugin: 隔離スタックの test-runner でアプリの JSON ログを stdout に出す。

既存の integration テストは ``configure_logging()`` を呼ばない（テストの中身は変えない）。
test-runner では ``pytest -p apptest_logging_plugin -s`` で起動し、pytest プロセス内で動く
Activity・Workflow・repository のログを、本番と同じ整形器で stdout（→ json-file → Collector）へ
流す。A の ``infrastructure.logging.configure_logging`` が無ければ何もしない（A 実装前でも既存
テストの結果を変えない）。

compose.apptest.yaml が ``PYTHONPATH=/src:/src/deploy/logging/test`` を渡す。
"""

from __future__ import annotations

import sys


def pytest_configure(config) -> None:  # noqa: ARG001 - pytest の hook 名と引数
    try:
        from infrastructure.logging import configure_logging  # type: ignore[import-not-found]
    except ImportError:
        sys.stderr.write("apptest_logging_plugin: configure_logging が無い（A 未実装）\n")
        return
    configure_logging()
