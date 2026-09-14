"""レイヤ依存の検査（INV-3 / INV-6）。

このテストは Phase 0 の時点で既に有効である。パッケージが空のうちは自明に通るが、
最初の違反importが入った瞬間に落ちる。「実装してから検査を足す」を避けるための配置。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# 各レイヤがimportしてよいトップレベルパッケージ（docs/architecture/overview.md の表）
ALLOWED: dict[str, set[str]] = {
    "contracts": set(),
    "domain": {"contracts"},
    "infrastructure": {"contracts", "domain"},  # ADR-0007
    "workers": {"domain", "contracts", "infrastructure"},
    "apps": {"domain", "contracts", "infrastructure"},
}
LAYERS = set(ALLOWED)


def _python_files(layer: str) -> list[pathlib.Path]:
    return sorted((REPO / layer).rglob("*.py"))


def _imported_roots(path: pathlib.Path) -> set[tuple[str, str]]:
    """(トップレベルパッケージ, 完全なモジュール名) の集合を返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add((node.module.split(".")[0], node.module))
    return found


def test_layer_dependencies_are_one_directional() -> None:
    """INV-6: 依存は overview.md の表の向きだけ。"""
    violations: list[str] = []
    for layer, allowed in ALLOWED.items():
        for path in _python_files(layer):
            for root, module in _imported_roots(path):
                if root in LAYERS and root != layer and root not in allowed:
                    rel = path.relative_to(REPO)
                    violations.append(f"{rel}: {layer} must not import {module} (INV-6)")
    assert not violations, "\n".join(violations)


def test_workers_do_not_import_other_workers() -> None:
    """INV-3: worker同士は疎結合。共有したいものは domain/ か infrastructure/ へ降ろす。"""
    violations: list[str] = []
    for path in _python_files("workers"):
        rel = path.relative_to(REPO)
        own = rel.parts[1] if len(rel.parts) > 2 else None
        if own is None:
            continue
        for _root, module in _imported_roots(path):
            parts = module.split(".")
            if parts[0] == "workers" and len(parts) > 1 and parts[1] != own:
                violations.append(f"{rel}: imports {module} (INV-3)")
    assert not violations, "\n".join(violations)


def test_domain_has_no_io_dependencies() -> None:
    """INV-6: domain/ は純粋。DB・HTTP・Temporal・ファイルI/Oに触れない。"""
    banned = {
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "psycopg2",
        "temporalio",
        "fastapi",
        "starlette",
        "httpx",
        "requests",
        "boto3",
        "minio",
        "aiofiles",
        # メディアのデコード・生成ライブラリ（ADR-0017）
        "PIL",
        "av",
        "piper",
        "numpy",
        "fal_client",
    }
    violations: list[str] = []
    for path in _python_files("domain"):
        for root, module in _imported_roots(path):
            if root in banned:
                violations.append(f"{path.relative_to(REPO)}: imports {module} (INV-6)")
    assert not violations, "\n".join(violations)


def _imports_under(path: pathlib.Path, prefix: str) -> list[str]:
    return [
        module
        for _root, module in _imported_roots(path)
        if module == prefix or module.startswith(prefix + ".")
    ]


def test_storyboard_and_planning_workers_do_not_import_each_other() -> None:
    """INV-3 を storyboard 導入の組み合わせで名指しで固定する（一般規則の上乗せ）。"""
    violations: list[str] = []
    for own, other in (("storyboard", "planning"), ("planning", "storyboard")):
        for path in _python_files(f"workers/{own}"):
            for module in _imports_under(path, f"workers.{other}"):
                violations.append(f"{path.relative_to(REPO)}: imports {module} (INV-3)")
    assert _python_files("workers/storyboard"), "workers/storyboard が見つからない"
    assert not violations, "\n".join(violations)


def test_apps_do_not_import_the_openmontage_adapter_or_subprocess() -> None:
    """API は起動するだけ。外部仕様の読み込みも子プロセスも worker 側（INV-16 / ADR-0016）。"""
    violations: list[str] = []
    for path in _python_files("apps"):
        for prefix in ("infrastructure.providers.openmontage_storyboard", "subprocess"):
            for module in _imports_under(path, prefix):
                violations.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not violations, "\n".join(violations)


def test_render_adapter_is_confined_to_workers() -> None:
    """ffmpeg の子プロセスを起動する render adapter は worker 側だけが使う（INV-16 / Phase 5）。"""
    violations: list[str] = []
    for layer in ("apps", "domain", "contracts"):
        for path in _python_files(layer):
            for module in _imports_under(path, "infrastructure.render"):
                violations.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not violations, "\n".join(violations)


PRODUCTION_WORKERS = ("production", "production_image", "production_voice", "production_video")


def test_production_workers_do_not_import_each_other_or_earlier_stages() -> None:
    """ADR-0017: workflow とメディア別 worker は contracts の名前と型だけを共有する（INV-3）。"""
    violations: list[str] = []
    for own in PRODUCTION_WORKERS:
        others = [w for w in PRODUCTION_WORKERS if w != own] + ["storyboard", "planning"]
        for path in _python_files(f"workers/{own}"):
            for other in others:
                for module in _imports_under(path, f"workers.{other}"):
                    violations.append(f"{path.relative_to(REPO)}: imports {module} (INV-3)")
    assert not violations, "\n".join(violations)


def test_apps_do_not_import_providers_or_media() -> None:
    """API は起動するだけ。provider adapter とメディア処理は worker 側（INV-16 / ADR-0017）。"""
    violations: list[str] = []
    for path in _python_files("apps"):
        for prefix in ("infrastructure.providers", "infrastructure.media", "PIL", "av"):
            for module in _imports_under(path, prefix):
                violations.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not violations, "\n".join(violations)


def test_media_libraries_are_confined_to_infrastructure_media() -> None:
    """Pillow / PyAV を直接使うのは infrastructure/media だけ（テストの fake を除く）。"""
    violations: list[str] = []
    for layer in ("apps", "workers", "domain", "infrastructure", "contracts"):
        for path in _python_files(layer):
            rel = path.relative_to(REPO)
            if rel.parts[:2] == ("infrastructure", "media"):
                continue
            for prefix in ("PIL", "av"):
                for module in _imports_under(path, prefix):
                    violations.append(f"{rel}: imports {module} (ADR-0017)")
    assert not violations, "\n".join(violations)


#: domain が知ってはならない provider / 実装ツールの名前（小文字の語）。
PROVIDER_TOKENS: frozenset[str] = frozenset(
    {"fal", "seedream", "seedance", "piper", "ffmpeg", "kling", "veo", "pillow", "pyav"}
)


def provider_name_tokens(text: str) -> set[str]:
    """識別子・文字列・コメントを語に割り、provider 名と一致する語を返す。

    - 英数字以外（``_`` ``-`` ``.`` 空白など）で区切る: ``fal_image`` → ``fal`` / ``image``
    - camelCase / PascalCase の境界で区切る: ``FalImageGenerator`` → ``fal`` / ``image`` / ...
    - 大小文字を無視し、**語全体**で照合する: ``false`` / ``falsy`` / ``vegetable`` / ``veon`` は
      一致しない（部分文字列で探すと ``false`` が ``fal`` に誤一致する）
    """
    import re

    found: set[str] = set()
    for chunk in re.split(r"[^0-9A-Za-z]+", text):
        if not chunk:
            continue
        # ABCWord → ABC / Word、fooBar → foo / Bar、fal2 → fal / 2
        words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", chunk)
        for word in words:
            lowered = word.lower()
            if lowered in PROVIDER_TOKENS:
                found.add(lowered)
    return found


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fal_image", {"fal"}),
        ("FalImageGenerator", {"fal"}),
        ("FAL_VIDEO", {"fal"}),
        ("fal-client", {"fal"}),
        ("FalQueueClient", {"fal"}),
        ("SeedreamV4", {"seedream"}),
        ("seedance-1-pro", {"seedance"}),
        ("PiperVoice", {"piper"}),
        ("run ffmpeg -i", {"ffmpeg"}),
        ("KLING", {"kling"}),
        ("google_veo3", {"veo"}),
        ("value = False", set()),
        ("falsy fallback", set()),
        ("vegetable veon", set()),
        ("pipers skipper", set()),
        ("default_value", set()),
    ],
)
def test_provider_name_matcher(text: str, expected: set[str]) -> None:
    assert provider_name_tokens(text) == expected


def test_domain_does_not_name_media_providers_or_tools() -> None:
    """domain は provider / 実装ツールの名前を知らない（ADR-0017）。"""
    violations: list[str] = []
    for path in _python_files("domain"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            names = provider_name_tokens(line)
            if names:
                violations.append(
                    f"{path.relative_to(REPO)}:{lineno}: {sorted(names)}: {line.strip()}"
                )
    assert not violations, "\n".join(violations)
