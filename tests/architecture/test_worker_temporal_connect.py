"""Worker は Temporal へ直接 Client.connect せず、connect_with_retry を使う。"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(ROOT.glob("workers/**/run_worker.py")) + [
    ROOT / "tests/support/fake_upload_worker.py"
]


def test_files_found() -> None:
    assert len(FILES) >= 11


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_uses_connect_with_retry(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "Client.connect(" not in text
    assert "connect_with_retry" in text
