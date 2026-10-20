"""release preflight は読み取り専用（INV-65 / ADR-0049）。

collector が呼べる外部コマンドと SQL を許可リストで固定する。書き込み系の動詞が増えたら落ちる。
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
FILES = [
    REPO / "domain" / "release_preflight.py",
    REPO / "infrastructure" / "release" / "collectors.py",
    REPO / "infrastructure" / "release" / "compose_queues.py",
    REPO / "scripts" / "release-preflight.py",
]
#: collector が組み立ててよい docker のサブコマンド（inspect / ps だけ。restart・up・exec は不可）
DOCKER_SUBCOMMANDS = {"inspect", "ps"}
#: git の読み取り系だけ
GIT_SUBCOMMANDS = {"cat-file", "rev-parse", "status", "merge-base", "ls-tree", "show", "remote"}
GH_ALLOWED_PREFIX = ("api",)
FORBIDDEN_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b|\.(unpause|pause|trigger|terminate|signal"
    r"|start_workflow|execute_workflow|update|delete|toggle)\(|--method\s+(POST|PUT|PATCH|DELETE)|-X\s+(POST|PUT|PATCH|DELETE)",
    re.IGNORECASE,
)


def _sources() -> dict[pathlib.Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in FILES if p.exists()}


def _io_sources() -> dict[pathlib.Path, str]:
    """純粋な判定（domain）は破壊的 SQL を「検出する」正規表現を持つので、実行側だけを走査する。"""
    return {p: t for p, t in _sources().items() if p.name != "release_preflight.py"}


def test_the_domain_module_is_pure() -> None:
    tree = ast.parse((REPO / "domain" / "release_preflight.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    io = {"subprocess", "socket", "psycopg", "sqlalchemy", "temporalio", "httpx", "os", "pathlib"}
    assert not imported & io, imported & io


def test_the_release_preflight_files_exist() -> None:
    assert all(p.exists() for p in FILES), [str(p) for p in FILES if not p.exists()]


def test_no_write_verbs_in_sql_or_temporal_calls() -> None:
    bad: list[str] = []
    for path, text in _io_sources().items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            # docstring / コメントではなく、実際に値として使われる文字列と呼び出しだけを見る
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and FORBIDDEN_WORDS.search(node.value)
                and not _is_docstring(tree, node)
            ):
                bad.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "unpause",
                "pause",
                "trigger",
                "terminate",
                "signal",
                "start_workflow",
                "toggle",
            }:
                bad.append(f"{path.name}:{node.lineno}: .{node.attr}")
    assert not bad, "\n".join(bad)


def _is_docstring(tree: ast.AST, node: ast.Constant) -> bool:
    return any(isinstance(p, ast.Expr) and p.value is node for p in ast.walk(tree))


def test_subprocess_commands_are_read_only_allowlist() -> None:
    source = (REPO / "infrastructure" / "release" / "collectors.py").read_text(encoding="utf-8")
    docker = set(
        re.findall(
            r'"docker",\s*(?:"compose",\s*)?(?:"--profile",\s*"core",\s*)?"([a-z-]+)"', source
        )
    )
    git = set(re.findall(r'"git",\s*"([a-z-]+)"', source))
    assert docker <= DOCKER_SUBCOMMANDS, docker
    assert git <= GIT_SUBCOMMANDS, git
    assert docker and git


def test_no_docker_restart_up_down_in_any_release_file() -> None:
    for path, text in _sources().items():
        for verb in ("restart", "up", "down", "stop", "kill", "rm", "exec"):
            assert not re.search(rf'"docker",[^)]*"{verb}"', text), f"{path.name}: docker {verb}"
