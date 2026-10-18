"""Agent Tool の境界（ADR-0047、INV-61 / INV-62）。AST で見る（動的 import・推移的な依存は見ない）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import ast
import pathlib

from contracts.agent_tools import ToolId

REPO = pathlib.Path(__file__).resolve().parents[2]

#: tool の executor を置く場所。ここから書き込み系の経路を持たない（INV-61）。
#: 既存の ``infrastructure/analytics/youtube_analytics.py`` は対象外（Planner 用の既存 adapter で、
#: 投稿側 scope の定数と tokeninfo 確認の POST を持つ。tool はその read 用の port 越しにだけ使う）
TOOL_IMPL_DIRS = ("infrastructure/agents/tools",)
WRITE_SCOPE_MARKERS = (
    "youtube.upload",
    "youtube.force-ssl",
    "youtubepartner",
    "yt-analytics-monetary",
    "youtube.channel-memberships",
)
WRITE_HTTP_METHODS = {"post", "put", "delete", "patch"}


def _files(*roots: str) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root in roots:
        base = REPO / root
        if base.exists():
            out += sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return out


def _tree(path: pathlib.Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _is_tool_literal(value: str) -> bool:
    name, _, version = value.partition("@")
    return name in {t.value for t in ToolId} and (not version or version.isdigit())


def test_tool_ids_are_not_written_as_literals_outside_contracts() -> None:
    """INV-62: tool の識別子は ``ToolId`` を import する。集合を書き直さない（AGENTS.md §8）。"""
    bad: list[str] = []
    for path in _files("apps", "workers", "infrastructure", "domain", "application"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _is_tool_literal(node.value)
            ):
                bad.append(f"{path.relative_to(REPO)}:{node.lineno} {node.value!r}")
    assert not bad, "\n".join(bad)


def test_the_literal_check_catches_a_hardcoded_tool_id() -> None:
    assert _is_tool_literal("search_trends") and _is_tool_literal("get_video_analytics@1")
    assert not _is_tool_literal("search_trends_extra") and not _is_tool_literal("hello")


def test_tool_implementations_have_no_write_scope_or_write_http_method() -> None:
    """INV-61: read-only。書き込み scope の要求・POST/PUT/DELETE/PATCH の呼び出しを持たない。"""
    bad: list[str] = []
    for path in _files(*TOOL_IMPL_DIRS):
        rel = path.relative_to(REPO)
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                if any(marker in lowered for marker in WRITE_SCOPE_MARKERS):
                    bad.append(f"{rel}:{node.lineno} write scope {node.value!r}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_HTTP_METHODS
            ):
                bad.append(f"{rel}:{node.lineno} .{node.func.attr}(...)")
    assert not bad, "\n".join(bad)


def test_the_write_check_catches_each_form() -> None:
    source = (
        "SCOPE = 'https://www.googleapis.com/auth/youtube.upload'\n"
        "client.post(url)\n"
        "client.get(url)\n"
    )
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found += [m for m in WRITE_SCOPE_MARKERS if m in node.value.lower()]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITE_HTTP_METHODS
        ):
            found.append(node.func.attr)
    assert found == ["youtube.upload", "post"]


def test_the_tool_registry_and_domain_ports_stay_pure() -> None:
    """tool の契約・port は I/O を持たない（domain は infrastructure を import しない）。"""
    allowed_roots = {"contracts", "domain", "pydantic", "__future__"}
    stdlib_ok = {
        "asyncio",
        "contextlib",
        "dataclasses",
        "datetime",
        "decimal",
        "collections",
        "enum",
        "hashlib",
        "json",
        "time",
        "typing",
        "uuid",
    }
    bad: list[str] = []
    for path in [REPO / "contracts/agent_tools.py", REPO / "contracts/agent_tool_registry.py"] + (
        _files("domain/agents/tools") + [REPO / "domain/agents/tool_gateway.py"]
    ):
        for node in ast.walk(_tree(path)):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".")[0]
                if root not in allowed_roots | stdlib_ok:
                    bad.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not bad, "\n".join(bad)
