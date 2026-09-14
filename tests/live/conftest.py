"""live テストの三重の隔離のうち 1 と 2（AGENTS.md §9 / INV-18）。

1. ``AVP_LIVE_CODEX=1`` が無ければ、このディレクトリのモジュールを **import すらしない**
2. 収集されたテストには ``live`` マーカーを自動で付ける
3. （``pyproject.toml``）``addopts = "-m 'not live'"`` で既定実行から除外する
"""

from __future__ import annotations

import os

import pytest

LIVE_ENV_VAR = "AVP_LIVE_CODEX"

#: 有料画像 provider の live テスト（tests/live/test_fal_image_live.py）のスイッチ。
LIVE_FAL_ENV_VAR = "AVP_LIVE_FAL"

_ENABLED = os.environ.get(LIVE_ENV_VAR) == "1" or os.environ.get(LIVE_FAL_ENV_VAR) == "1"
collect_ignore_glob = ["*"] if not _ENABLED else []


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if "tests/live/" in item.nodeid or item.nodeid.startswith("tests/live"):
            item.add_marker(pytest.mark.live)
