"""``tests/web/*.test.mjs``（UI の純ロジック）を ``node --test`` で走らせる薄い wrapper。

node が無い環境（CI を含む）では skip する。Node 22 では ``node --test <dir>`` が使えないため
glob を渡す。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_TESTS = sorted((ROOT / "tests" / "web").glob("*.test.mjs"))


def test_web_tests_exist() -> None:
    assert WEB_TESTS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_web_tests_pass() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--test", *map(str, WEB_TESTS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
