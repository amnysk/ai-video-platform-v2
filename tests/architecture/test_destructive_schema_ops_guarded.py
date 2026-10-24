"""破壊的スキーマ操作は単一ガードの外で呼ばない（INV-69 / ADR-0055）。

対象: ``drop_all`` / ``create_all`` / ``DROP SCHEMA|DATABASE``。

- 実行コード（apps / workers / domain / infrastructure / scripts）:
  ``infrastructure/db/safety.py`` 以外で禁止。
- tests: 次のどちらかを満たすファイルだけ許す。
  (a) SQLite のインメモリDBだけを使う（``sqlite`` を含み、PostgreSQL の DROP が無い）
  (b) ``assert_destructive_allowed`` / ``guarded_*`` を呼ぶ
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "infrastructure" / "db" / "safety.py"
RUNTIME_DIRS = ("apps", "workers", "domain", "infrastructure", "scripts", "contracts")
SCHEMA_OP = re.compile(r"\.(drop_all|create_all)\b|\bDROP\s+(SCHEMA|DATABASE)\b", re.IGNORECASE)
PG_DESTRUCTIVE = re.compile(r"\bDROP\s+(SCHEMA|DATABASE)\b", re.IGNORECASE)
GUARD_CALL = re.compile(r"\b(assert_destructive_allowed|guarded_drop_all|guarded_create_all)\(")


def _files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if ".venv" not in p.parts]


def test_runtime_code_has_no_unguarded_schema_ops() -> None:
    offenders: list[str] = []
    for d in RUNTIME_DIRS:
        for path in _files(ROOT / d):
            if path == GUARD:
                continue
            text = path.read_text(encoding="utf-8")
            for m in SCHEMA_OP.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {m.group(0)}")
    assert not offenders, "use infrastructure.db.safety.guarded_*:\n" + "\n".join(offenders)


def test_test_code_schema_ops_are_sqlite_only_or_guarded() -> None:
    offenders: list[str] = []
    this = Path(__file__).resolve()
    for path in _files(ROOT / "tests"):
        if path.resolve() == this:
            continue
        text = path.read_text(encoding="utf-8")
        if not SCHEMA_OP.search(text):
            continue
        guarded = bool(GUARD_CALL.search(text))
        sqlite_only = "sqlite" in text.lower() and not PG_DESTRUCTIVE.search(text)
        if not (guarded or sqlite_only):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "call assert_destructive_allowed before schema ops:\n" + "\n".join(
        offenders
    )


def test_checker_detects_an_unguarded_call() -> None:
    assert SCHEMA_OP.search("await conn.run_sync(Base.metadata.drop_all)")
    assert SCHEMA_OP.search("text(f'DROP SCHEMA \"{s}\" CASCADE')")
    assert not GUARD_CALL.search("Base.metadata.drop_all")
