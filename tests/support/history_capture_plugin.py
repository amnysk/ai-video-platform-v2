"""既存の workflow テストが走らせた workflow の履歴を JSON に書き出す pytest plugin。

ログ導入前（01eb0ee）のコードで旧履歴を採り、新しいコードの Replayer で replay する
（INV-40 / レビュー I-3。``tests/unit/test_log_old_history_replay.py``）ための道具。

    AVP_CAPTURE_HISTORY_DIR=/tmp/histories \\
        pytest -p tests.support.history_capture_plugin tests/unit/test_render_workflow.py

``WorkflowHandle.result()`` が返った（または失敗した）直後に ``fetch_history()`` し、
``<workflow type>__<test name>__<n>.json`` に保存する。既定の実行では読み込まれない。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

CAPTURE_DIR_ENV = "AVP_CAPTURE_HISTORY_DIR"
_current = {"test": "unknown", "n": 0}


def _install(directory: Path) -> None:
    from temporalio.client import WorkflowHandle

    original = WorkflowHandle.result

    async def result(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await original(self, *args, **kwargs)
        finally:
            try:
                history = await self.fetch_history()
                wf_type = history.events[
                    0
                ].workflow_execution_started_event_attributes.workflow_type.name
                _current["n"] += 1
                name = f"{wf_type}__{_current['test']}__{_current['n']}.json"
                (directory / re.sub(r"[^\w.-]", "_", name)).write_text(
                    history.to_json(), encoding="utf-8"
                )
            except Exception:
                pass

    WorkflowHandle.result = result  # type: ignore[method-assign]


def pytest_configure(config: pytest.Config) -> None:
    target = os.environ.get(CAPTURE_DIR_ENV)
    if target:
        path = Path(target)
        path.mkdir(parents=True, exist_ok=True)
        _install(path)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _current["test"] = item.name
    _current["n"] = 0
