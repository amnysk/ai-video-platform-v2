"""integration テストの Temporal client は tests/support/temporal.py の helper からだけ作る。

直接 ``Client.connect`` すると namespace の既定（``default`` = 本番 worker の namespace）や
書き写した ``namespace="default"`` で本番 namespace に workflow を起動してしまう（ADR-0021 追補）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tests/support/temporal.py"
#: 産業用 fake（本物の worker と同じ Settings の namespace で起動する）。integration は使わない
EXEMPT = {ROOT / "tests/support/fake_upload_worker.py"}

FILES = sorted(
    p
    for p in [*ROOT.glob("tests/integration/**/*.py"), *ROOT.glob("tests/support/**/*.py")]
    if p != HELPER and p not in EXEMPT
)

FORBIDDEN = [
    re.compile(r"Client\.connect\("),
    re.compile(r"connect_with_retry"),
    re.compile(r"namespace\s*=\s*[\"']default[\"']"),
]


def test_files_found() -> None:
    assert len(FILES) >= 20
    assert "Client.connect(" in HELPER.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_direct_temporal_client(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [pat.pattern for pat in FORBIDDEN if pat.search(text)]
    assert not hits, f"{path.relative_to(ROOT)}: connect_test_client を使うこと: {hits}"


def test_integration_conftest_guards_namespace_at_collection() -> None:
    text = (ROOT / "tests/integration/conftest.py").read_text(encoding="utf-8")
    assert "require_test_temporal_namespace()" in text
    assert "require_test_minio_bucket()" in text
