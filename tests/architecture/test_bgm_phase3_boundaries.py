"""BGM Phase 3 の境界（INV-135 / INV-137、ADR-0083）。

- 新しい BGM の部品は production / 課金 provider を import しない
- 校正した定数は contracts/music_render.py にだけ定義する（AGENTS.md §8）
- Render worker は新しい部品を加えても fal / production のコードを読み込まない
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

PHASE3_MODULES = [
    "domain/music/placement.py",
    "domain/music/intelligibility.py",
    "domain/music/quality_checks.py",
    "infrastructure/music/catalog_store.py",
    "infrastructure/render/bgm_intelligibility.py",
    # ADR-0087: 自動選曲・DB カタログ・梯子・取込（fal / production を import しない）
    "domain/music/lexicon.py",
    "domain/music/requirements.py",
    "domain/music/scoring.py",
    "domain/music/auto_select.py",
    "infrastructure/music/db_source.py",
    "infrastructure/db/bgm_repositories.py",
    "workers/render/bgm_ladder.py",
    "scripts/bgm_catalog_import.py",
]
FORBIDDEN_PREFIXES = (
    "domain.production",
    "domain.provider",
    "workers.production",
    "workers.generation",
    "infrastructure.production",
    "infrastructure.providers",
    "contracts.production_activities",
)
#: Render worker が読み込んではならない（fal クライアントと production worker。既存の
#: test_bgm_render_boundaries と同じ範囲。domain.production の純粋関数は render 自身も使う）
LOADED_BY_RENDER_FORBIDDEN = (
    "infrastructure.providers",
    "workers.production",
    "workers.generation",
)
CALIBRATED = ("SPEECH_MARGIN_MIN_CENTI_LU", "VOICE_REFERENCE_LUFS_CENTI")


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_phase3_bgm_modules_do_not_import_production_or_provider_code() -> None:
    violations = [
        f"{rel}: imports {m}"
        for rel in PHASE3_MODULES
        for m in _imports(REPO / rel)
        if m.startswith(FORBIDDEN_PREFIXES)
    ]
    assert not violations, "\n".join(violations)


def test_the_calibrated_constants_are_defined_only_in_the_contract() -> None:
    """同じ値を別モジュールで書き直さない。定義（代入）は contracts/music_render.py に 1 つずつ。"""
    definitions: dict[str, list[str]] = {name: [] for name in CALIBRATED}
    for root in ("contracts", "domain", "infrastructure", "workers", "apps"):
        for path in sorted((REPO / root).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target]
                for t in targets:
                    if t.id in definitions:
                        definitions[t.id].append(str(path.relative_to(REPO)))
    assert definitions == {name: ["contracts/music_render.py"] for name in CALIBRATED}


def test_the_render_worker_still_loads_no_fal_or_production_module_with_the_new_parts() -> None:
    code = (
        "import sys\n"
        "import workers.render.run_worker  # noqa: F401\n"
        "import domain.music.placement, domain.music.intelligibility, domain.music.quality_checks\n"
        "import infrastructure.music.catalog_store, infrastructure.render.bgm_intelligibility\n"
        f"bad = [m for m in sys.modules if m.startswith({LOADED_BY_RENDER_FORBIDDEN!r})]\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.');" + code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
