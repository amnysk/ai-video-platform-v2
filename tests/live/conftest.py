"""live テストの三重の隔離のうち 1 と 2（AGENTS.md §9 / INV-18）。

1. ``AVP_LIVE_CODEX=1`` が無ければ Codex の live モジュールを、``AVP_LIVE_PIPER=1`` が無ければ
   Piper の live モジュールを **import すらしない**
2. 収集されたテストには ``live`` マーカーを自動で付ける
3. （``pyproject.toml``）``addopts = "-m 'not live'"`` で既定実行から除外する
"""

from __future__ import annotations

import fnmatch
import os
import pathlib

import pytest

LIVE_ENV_VAR = "AVP_LIVE_CODEX"
#: ローカル Piper（非課金）の live テストだけを有効にするスイッチ（Phase 4B）
LIVE_PIPER_ENV_VAR = "AVP_LIVE_PIPER"
PIPER_LIVE_GLOB = "test_piper_*.py"

collect_ignore_glob: list[str] = []
if os.environ.get(LIVE_ENV_VAR) != "1":
    # 許可リスト方式: Piper のモジュール以外は**全部**無視する（新しい live テストが黙って入らない）
    collect_ignore_glob.extend(
        path.name
        for path in pathlib.Path(__file__).parent.glob("*.py")
        if path.name != "conftest.py" and not fnmatch.fnmatch(path.name, PIPER_LIVE_GLOB)
    )
if os.environ.get(LIVE_PIPER_ENV_VAR) != "1":
    collect_ignore_glob.append(PIPER_LIVE_GLOB)


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if "tests/live/" in item.nodeid or item.nodeid.startswith("tests/live"):
            item.add_marker(pytest.mark.live)
