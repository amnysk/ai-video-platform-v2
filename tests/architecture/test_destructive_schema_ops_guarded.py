"""破壊的な DB 操作は、**呼び出し箇所ごとに**ガードの後でしか書かない（INV-69 / INV-83）。

対象（AST で見る。コメント・docstring・正規表現リテラルは対象外）:

- ``X.drop_all`` / ``X.create_all``（呼び出しも ``run_sync(Base.metadata.create_all)`` の参照も）
- ``X.drop_table`` / ``X.drop_schema``（``op.drop_table`` など）
- 先頭が ``DROP TABLE|SCHEMA|DATABASE`` / ``TRUNCATE`` の SQL 文字列（f 文字列も）

許す条件は **箇所ごと**（ファイル単位ではない）:

- ``infrastructure/db/safety.py``（ガード自身）と ``infrastructure/db/migrations/versions/``
  （alembic が自分の DB にだけ流す。走査は ADR-0050）
- 箇所を囲む **最も内側の関数**（無ければモジュール）の中で、その箇所より前に
  ``assert_destructive_allowed`` / ``assert_live_test_database`` / ``guarded_*`` の呼び出しがある
- tests 配下に限り、囲む関数が SQLite の engine を自分で作っている（``"sqlite..."`` の文字列があり
  ``postgresql`` の文字列が無い）

ファイルに1箇所ガードがあるだけで、別の関数の素の ``DROP TABLE`` が通る、という抜け穴は無い。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from infrastructure.db.safety import is_destructive_statement

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "infrastructure" / "db" / "safety.py"
MIGRATIONS = ROOT / "infrastructure" / "db" / "migrations" / "versions"
SCAN_DIRS = ("apps", "workers", "domain", "infrastructure", "scripts", "contracts", "tests")
GUARD_NAMES = frozenset(
    {
        "assert_destructive_allowed",
        "assert_live_test_database",
        "guarded_drop_all",
        "guarded_create_all",
        "guarded_drop_database",
    }
)
#: ガード自身の単体テスト。SQL を **文字列として** 純粋関数に渡すだけで、実行しない
GUARD_UNIT_TEST = ROOT / "tests" / "unit" / "test_db_destructive_guard.py"
SCHEMA_ATTRS = frozenset({"drop_all", "create_all", "drop_table", "drop_schema"})


@dataclass(frozen=True)
class Site:
    line: int
    what: str


def _static_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", "")


def _scopes(tree: ast.Module) -> dict[int, ast.AST]:
    """node の id -> それを囲む最も内側の関数（無ければ Module）。"""
    out: dict[int, ast.AST] = {}

    def walk(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = scope
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            walk(child, inner)

    walk(tree, tree)
    return out


def _docstrings(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _sites(tree: ast.Module) -> list[tuple[ast.AST, Site]]:
    docs = _docstrings(tree)
    parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    found: list[tuple[ast.AST, Site]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in SCHEMA_ATTRS:
            found.append((node, Site(node.lineno, f".{node.attr}")))
        elif isinstance(node, ast.Constant | ast.JoinedStr) and id(node) not in docs | parts:
            text = _static_text(node)
            if text is not None and is_destructive_statement(text):
                found.append((node, Site(node.lineno, f"SQL {text.strip()[:30]!r}")))
    return found


def _guarded_before(scope: ast.AST, line: int) -> bool:
    return any(
        isinstance(n, ast.Call) and _call_name(n) in GUARD_NAMES and n.lineno < line
        for n in ast.walk(scope)
    )


def _sqlite_only(scope: ast.AST) -> bool:
    texts = [
        n.value for n in ast.walk(scope) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    return any(t.startswith("sqlite") for t in texts) and not any("postgresql" in t for t in texts)


def unguarded_sites(source: str, *, in_tests: bool) -> list[Site]:
    tree = ast.parse(source)
    scopes = _scopes(tree)
    bad: list[Site] = []
    for node, site in _sites(tree):
        scope = scopes.get(id(node), tree)
        if _guarded_before(scope, site.line):
            continue
        if in_tests and _sqlite_only(scope):
            continue
        bad.append(site)
    return bad


def _files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        out += [p for p in (ROOT / d).rglob("*.py") if ".venv" not in p.parts]
    return out


def test_every_destructive_call_site_is_guarded() -> None:
    this = Path(__file__).resolve()
    offenders: list[str] = []
    for path in _files():
        resolved = path.resolve()
        if resolved in (GUARD, this, GUARD_UNIT_TEST) or MIGRATIONS in resolved.parents:
            continue
        rel = path.relative_to(ROOT)
        in_tests = rel.parts[0] == "tests"
        for site in unguarded_sites(path.read_text(encoding="utf-8"), in_tests=in_tests):
            offenders.append(f"{rel}:{site.line}: {site.what}")
    assert not offenders, (
        "破壊的操作の前に infrastructure.db.safety のガードを呼ぶ（箇所ごと）:\n"
        + "\n".join(offenders)
    )


def test_support_db_module_only_reexports_the_guard() -> None:
    """規則を別に持たない（AGENTS §8）: tests/support/db.py に関数・クラスの定義が無い。"""
    tree = ast.parse((ROOT / "tests" / "support" / "db.py").read_text(encoding="utf-8"))
    defined = [n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.ClassDef)]
    assert defined == []


# ---- 検査器自身の検査（箇所ごとに見分けること）

GUARDED = """
def a(conn):
    assert_destructive_allowed(conn.engine.url)
    conn.execute(text('DROP SCHEMA "s" CASCADE'))
"""
UNGUARDED_NEIGHBOUR = (
    GUARDED
    + """
def b(conn):
    conn.execute(text("DROP TABLE episodes"))
"""
)


def test_checker_flags_only_the_unguarded_function_in_a_file_with_a_guard() -> None:
    assert unguarded_sites(GUARDED, in_tests=False) == []
    sites = unguarded_sites(UNGUARDED_NEIGHBOUR, in_tests=False)
    assert [s.line for s in sites] == [7]


def test_checker_requires_the_guard_before_the_operation() -> None:
    late = (
        "def a(conn):\n    conn.execute(text('TRUNCATE jobs'))\n    assert_destructive_allowed(1)\n"
    )
    assert [s.line for s in unguarded_sites(late, in_tests=False)] == [2]


def test_checker_covers_every_destructive_form() -> None:
    forms = {
        "metadata.drop_all(conn)": ".drop_all",
        "conn.run_sync(Base.metadata.create_all)": ".create_all",
        "op.drop_table('t')": ".drop_table",
        "conn.execute(text('TRUNCATE t'))": "SQL",
        "conn.execute(text(f'DROP SCHEMA \"{s}\" CASCADE'))": "SQL",
        "conn.execute('DROP DATABASE x_test')": "SQL",
        "conn.execute('  drop table if exists t')": "SQL",
    }
    for body, what in forms.items():
        sites = unguarded_sites(
            f"def f(conn, op, metadata, Base, s):\n    {body}\n", in_tests=False
        )
        assert [x.what.split(" ")[0] for x in sites] == [what], body


def test_checker_ignores_docstrings_comments_and_non_sql_strings() -> None:
    src = (
        '"""DROP TABLE x"""\n'
        "# metadata.drop_all\n"
        "def f():\n"
        '    """TRUNCATE t is discussed here"""\n'
        '    return "SELECT 1", "see DROP TABLE docs", r"\\bDROP\\s+TABLE"\n'
    )
    assert unguarded_sites(src, in_tests=False) == []


def test_sqlite_exemption_is_per_function_and_tests_only() -> None:
    sqlite = (
        "def mk():\n"
        '    e = create_async_engine("sqlite+aiosqlite://")\n'
        "    conn.run_sync(Base.metadata.create_all)\n"
    )
    assert unguarded_sites(sqlite, in_tests=True) == []
    assert unguarded_sites(sqlite, in_tests=False) != []
    mixed = sqlite.replace('"sqlite+aiosqlite://"', '"sqlite://", "postgresql://x"')
    assert unguarded_sites(mixed, in_tests=True) != []
    other_function = sqlite + "def g():\n    conn.run_sync(Base.metadata.create_all)\n"
    assert [s.line for s in unguarded_sites(other_function, in_tests=True)] == [5]
