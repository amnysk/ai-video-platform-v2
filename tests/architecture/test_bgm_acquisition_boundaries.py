"""BGM 自動収集の境界（INV-152、ADR-0087 §3）。

- 音楽の収集でネットワークに触れてよいのは ``infrastructure/music/sources/`` だけ
- render / production / upload の経路は、収集・ソース・収集 worker を import しない
- ドメイン（``domain/music``）は I/O を持たない（test_music_boundaries と同じ規則）
- 収集 worker は task queue と flag の起動条件を持ち、Episode の workflow を登録しない
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

NETWORK = (
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "urllib3",
    "http",
    "socket",
    "ssl",
    "websockets",
)

#: ネットワークを import してはならない BGM / 描画の周辺。workers/upload は YouTube のために
#: httpx を使うので対象外（収集を import しないことだけ下で検査する）
NETWORK_FREE = [
    "domain/music",
    "contracts/music.py",
    "contracts/music_sources.py",
    "contracts/music_selection.py",
    "contracts/bgm_acquisition_activities.py",
    "infrastructure/music/acquisition.py",
    "infrastructure/music/audio_inspect.py",
    "infrastructure/music/catalog_store.py",
    "infrastructure/music/credits.py",
    "infrastructure/music/evidence.py",
    "infrastructure/music/guard.py",
    "infrastructure/music/quality.py",
    "infrastructure/render",
    "workers/render",
    "workers/bgm_acquisition",
    "workers/production",
    "workers/production_image",
    "workers/production_video",
    "workers/production_voice",
]

#: 収集を知ってはならない側
ACQUISITION_MODULES = (
    "infrastructure.music.sources",
    "infrastructure.music.acquisition",
    "workers.bgm_acquisition",
)
NOT_ALLOWED_TO_ACQUIRE = [
    "workers/render",
    "workers/upload",
    "workers/production",
    "workers/production_image",
    "workers/production_video",
    "workers/production_voice",
    "workers/pipeline",
    "workers/planning",
    "workers/storyboard",
    "infrastructure/render",
    "infrastructure/upload",
    "infrastructure/production",
    "domain",
]


def _files(rel: str) -> list[pathlib.Path]:
    path = REPO / rel
    return [path] if path.is_file() else sorted(path.rglob("*.py"))


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _is_network(module: str) -> bool:
    root = module.split(".")[0]
    return root in NETWORK


def test_sources_package_exists_and_is_the_network_touching_place() -> None:
    files = _files("infrastructure/music/sources")
    assert files
    assert any("httpx" in _imports(f) for f in files)


def test_only_the_sources_package_imports_a_network_library_for_music() -> None:
    violations: list[str] = []
    for rel in NETWORK_FREE:
        files = _files(rel)
        assert files, f"{rel} が無い（検査が空振りしないこと）"
        for path in files:
            for module in sorted(_imports(path)):
                if _is_network(module):
                    violations.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not violations, "\n".join(violations)


def test_sources_modules_other_than_commons_do_not_import_httpx() -> None:
    for path in _files("infrastructure/music/sources"):
        if path.name == "commons.py":
            continue
        assert not any(_is_network(m) for m in _imports(path)), path.name


def test_render_production_upload_do_not_import_acquisition() -> None:
    violations: list[str] = []
    for rel in NOT_ALLOWED_TO_ACQUIRE:
        for path in _files(rel):
            for module in sorted(_imports(path)):
                if any(module == m or module.startswith(m + ".") for m in ACQUISITION_MODULES):
                    violations.append(f"{path.relative_to(REPO)}: imports {module}")
    assert not violations, "\n".join(violations)


def test_acquisition_worker_registers_no_episode_workflow() -> None:
    forbidden = ("workers.render", "workers.upload", "workers.pipeline", "workers.production")
    for path in _files("workers/bgm_acquisition"):
        for module in _imports(path):
            assert not module.startswith(forbidden), f"{path.name}: {module}"


def test_acquisition_worker_checks_both_flags_before_connecting() -> None:
    text = (REPO / "workers/bgm_acquisition/run_worker.py").read_text(encoding="utf-8")
    assert "BGM_ACQUISITION_TASK_QUEUE" in text
    guard = text.index("require_enabled(settings)")
    assert guard < text.index("connect_with_retry(settings)")
    assert "BGM_ACQUISITION_SETTING_KEY" in text and "BGM_SETTING_KEY" in text
    assert "Client.connect(" not in text


def test_acquisition_service_has_no_clock_or_random_of_its_own() -> None:
    """時計は注入（``clock``）。service が ``datetime.now`` を直接呼ばない。"""
    text = (REPO / "infrastructure/music/acquisition.py").read_text(encoding="utf-8")
    assert "datetime.now" not in text and "time.time" not in text and "import random" not in text


def test_the_only_originals_and_evidence_prefixes() -> None:
    text = (REPO / "infrastructure/music/acquisition.py").read_text(encoding="utf-8")
    assert 'EVIDENCE_PREFIX = "music/evidence/"' in text
    assert (
        "ORIGINALS_PREFIX" in text
    )  # 原本の接頭辞は domain.music.ingest の定義を使う（AGENTS.md §8）
