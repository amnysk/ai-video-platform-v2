"""運用者 UI のブラウザ E2E（``tests/e2e_ui/*.test.mjs``）を pytest から走らせる wrapper。

本物の FastAPI app を fake port でプロセス内起動し、headless Chrome で操作する
（理由は docs/testing/jpmas2-p2b-rationale.md §7）。node・Chrome・``playwright-core``
（``cd tests/e2e_ui && npm ci``）のどれかが無ければ skip する。有料 API・DB には触れない。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPECS = sorted(HERE.glob("*.test.mjs"))
CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def _chrome_available() -> bool:
    if any(shutil.which(c) for c in CHROME_CANDIDATES):
        return True
    cache = Path.home() / ".cache" / "ms-playwright"
    return cache.is_dir() and any(cache.glob("chromium-*/chrome-linux*/chrome"))


def test_e2e_specs_exist_and_lockfile_is_committed() -> None:
    assert len(SPECS) >= 4
    assert (HERE / "package.json").is_file()
    assert (HERE / "package-lock.json").is_file()  # 版を固定する


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(
    not (HERE / "node_modules" / "playwright-core").is_dir(),
    reason="playwright-core is not installed (cd tests/e2e_ui && npm ci)",
)
@pytest.mark.skipif(not _chrome_available(), reason="Chrome/Chromium is not installed")
def test_browser_e2e_passes() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--test", *map(str, SPECS)],
        cwd=HERE.parents[1],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
