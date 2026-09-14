"""live テストの三重の隔離のうち 1 と 2（AGENTS.md §9 / INV-18）。

1. provider 別スイッチが無ければ live モジュールを **import すらしない**
   （Codex: ``AVP_LIVE_CODEX=1`` / Piper: ``AVP_LIVE_PIPER=1`` / fal: ``AVP_LIVE_FAL=1`` /
   YouTube: ``AVP_LIVE_YOUTUBE=1``。実アップロードはさらに ``CONFIRM_UPLOAD=1``）
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

#: 有料画像/動画 provider（fal）の live テストのスイッチ（Phase 4A/4C）
LIVE_FAL_ENV_VAR = "AVP_LIVE_FAL"
FAL_LIVE_GLOB = "test_fal_*.py"

#: 実 YouTube（Phase 6）の live テストのスイッチ。投稿は private のみ
LIVE_YOUTUBE_ENV_VAR = "AVP_LIVE_YOUTUBE"
YOUTUBE_LIVE_GLOB = "test_youtube_*.py"

#: provider 別の許可リスト。各 glob は対応するスイッチが "1" のときだけ収集する。
#: どの glob にも当たらないモジュールは Codex 扱い（AVP_LIVE_CODEX）で、黙って有効にはならない。
_SWITCHED_GLOBS = {
    PIPER_LIVE_GLOB: LIVE_PIPER_ENV_VAR,
    FAL_LIVE_GLOB: LIVE_FAL_ENV_VAR,
    YOUTUBE_LIVE_GLOB: LIVE_YOUTUBE_ENV_VAR,
}

collect_ignore_glob: list[str] = [
    glob for glob, env in _SWITCHED_GLOBS.items() if os.environ.get(env) != "1"
]
if os.environ.get(LIVE_ENV_VAR) != "1":
    collect_ignore_glob.extend(
        path.name
        for path in pathlib.Path(__file__).parent.glob("*.py")
        if path.name != "conftest.py"
        and not any(fnmatch.fnmatch(path.name, glob) for glob in _SWITCHED_GLOBS)
    )


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if "tests/live/" in item.nodeid or item.nodeid.startswith("tests/live"):
            item.add_marker(pytest.mark.live)
