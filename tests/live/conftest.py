"""live テストの三重の隔離のうち 1 と 2（AGENTS.md §9 / INV-18）。

1. ``AVP_LIVE_CODEX=1`` が無ければ、このディレクトリのモジュールを **import すらしない**
2. 収集されたテストには ``live`` マーカーを自動で付ける
3. （``pyproject.toml``）``addopts = "-m 'not live'"`` で既定実行から除外する
"""

from __future__ import annotations

import os

import pytest

LIVE_ENV_VAR = "AVP_LIVE_CODEX"

collect_ignore_glob = ["*"] if os.environ.get(LIVE_ENV_VAR) != "1" else []


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if "tests/live/" in item.nodeid or item.nodeid.startswith("tests/live"):
            item.add_marker(pytest.mark.live)
