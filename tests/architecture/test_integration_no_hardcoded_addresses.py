"""integration テストと support は接続先の住所を直書きしない（ADR-0079 / INV-122）。

本番スタックは同じホストの ``localhost:7233`` / ``:5432`` / ``:9000`` で公開されている。
直書きはその本番へ繋がる。接続先は環境変数（隔離スタック / CI が設定）からだけ読み、
conftest の収集時 guard（tests/support/production_guard.py）が宣言を検査する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "tests/support/production_guard.py"
FILES = sorted(
    p
    for p in [*ROOT.glob("tests/integration/**/*.py"), *ROOT.glob("tests/support/**/*.py")]
    if p != GUARD
)
HOSTS = r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|192\.168\.\d+\.\d+|host\.docker\.internal)"
FORBIDDEN = [
    re.compile(HOSTS + r":(?:7233|5432|9000|8000)\b"),
    re.compile(r"""TEMPORAL_ADDRESS\s*=\s*["']"""),
    re.compile(r"""\bconnect_test_client\(\s*["']"""),
    re.compile(r"@" + HOSTS + r"[:/]"),  # DB URL の host
]


def test_files_found() -> None:
    assert len(FILES) >= 20


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_hardcoded_connection_address(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [pat.pattern for pat in FORBIDDEN if pat.search(text)]
    assert not hits, f"{path.relative_to(ROOT)}: 接続先を直書きしない: {hits}"


def test_the_detector_catches_the_original_incident() -> None:
    assert any(p.search('TEMPORAL_ADDRESS = "localhost:7233"') for p in FORBIDDEN)
    assert any(p.search("postgresql://u:p@localhost:5432/avp_test") for p in FORBIDDEN)
    assert not any(p.search("temporal:7233") for p in FORBIDDEN)


def test_integration_conftest_asserts_isolation_before_anything_else() -> None:
    text = (ROOT / "tests/integration/conftest.py").read_text(encoding="utf-8")
    assert "assert_isolated_targets(os.environ)" in text
    assert "assert_temporal_is_not_production" in text
    assert text.index("assert_isolated_targets") < text.index("require_test_temporal_namespace()")
