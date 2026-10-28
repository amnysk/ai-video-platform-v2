"""BGM ドメインの境界（INV-97 / INV-99 / INV-100、ADR-0072）。

domain/music は純粋で、production / provider（課金）側から import されない。
cue sheet は production の Artifact 契約に依存しない（BGM だけの差替えで fal を呼ばない前提）。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: I/O・時計・乱数・プロセスに触れる標準ライブラリ。domain/music では import しない。
BANNED_STDLIB = {
    "os", "pathlib", "io", "socket", "subprocess", "shutil", "tempfile", "urllib", "http",
    "ssl", "sqlite3", "asyncio", "threading", "multiprocessing", "random", "secrets", "time",
    "logging", "glob", "fileinput", "ftplib", "smtplib",
}  # fmt: skip

#: BGM を知ってはならない側（production・課金 provider・生成・upload の実行経路）。
FORBIDDEN_IMPORTERS = [
    "domain/production",
    "domain/provider",
    "workers/production",
    "workers/production_image",
    "workers/production_video",
    "workers/production_voice",
    "workers/generation",
    "infrastructure/production",
    "infrastructure/providers",
    "contracts/production_activities.py",
]

#: cue sheet 契約が依存してよい contracts（共有の型・パターンだけ）。
CONTRACT_ALLOWED = {"contracts.artifact_refs"}


def _files(rel: str) -> list[pathlib.Path]:
    path = REPO / rel
    return [path] if path.is_file() else sorted(path.rglob("*.py"))


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_domain_music_exists() -> None:
    assert _files("domain/music"), "domain/music が無い（検査が空振りしないこと）"


def test_domain_music_has_no_io_or_clock_imports() -> None:
    violations = [
        f"{p.relative_to(REPO)}: imports {m}"
        for p in _files("domain/music")
        for m in _imports(p)
        if m.split(".")[0] in BANNED_STDLIB
    ]
    assert not violations, "\n".join(violations)


def test_domain_music_does_not_read_the_clock_or_open_files() -> None:
    violations: list[str] = []
    for path in _files("domain/music"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"now", "utcnow", "today", "open", "read_text", "read_bytes"}:
                violations.append(f"{path.relative_to(REPO)}:{node.lineno}: calls {name}()")
    assert not violations, "\n".join(violations)


def test_domain_music_imports_only_contracts_and_itself() -> None:
    violations = [
        f"{p.relative_to(REPO)}: imports {m}"
        for p in _files("domain/music")
        for m in _imports(p)
        if m.split(".")[0] in {"domain", "infrastructure", "workers", "apps"}
        and not (m == "domain.music" or m.startswith("domain.music."))
    ]
    assert not violations, "\n".join(violations)


def test_production_and_provider_billing_modules_do_not_import_music() -> None:
    violations: list[str] = []
    for rel in FORBIDDEN_IMPORTERS:
        for path in _files(rel):
            for module in _imports(path):
                if module in {"contracts.music", "domain.music"} or module.startswith(
                    "domain.music."
                ):
                    violations.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not violations, "\n".join(violations)


def test_music_contract_does_not_depend_on_production_artifacts() -> None:
    """INV-100: cue sheet は production Artifact の契約を import しない。"""
    violations = [
        f"contracts/music.py: imports {m}"
        for m in _imports(REPO / "contracts" / "music.py")
        if m.split(".")[0] == "contracts" and m not in CONTRACT_ALLOWED | {"contracts.music"}
    ]
    assert not violations, "\n".join(violations)
