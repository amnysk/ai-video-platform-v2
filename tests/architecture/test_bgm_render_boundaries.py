"""BGM の Render 結線の境界（INV-113 / INV-116、ADR-0077）。

- 設定キー bgm_enabled を読む場所は 1 つ（worker の組み立て）
- Render の経路は fal / production のコードを一切読み込まない（BGM だけの変更が課金へ届かない）
- Temporal の Activity 名と入出力 dataclass のフィールドは導入前から不変（replay 互換）
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import subprocess
import sys

from contracts import render_activities as ra

REPO = pathlib.Path(__file__).resolve().parents[2]

BGM_MODULES = [
    "contracts/music_render.py",
    "domain/render/bgm.py",
    "domain/render/loudness.py",
    "infrastructure/render/bgm_source.py",
    "infrastructure/render/loudness.py",
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
DOMAIN_BANNED_STDLIB = {
    "os", "pathlib", "io", "socket", "subprocess", "shutil", "tempfile", "urllib", "http",
    "sqlite3", "asyncio", "threading", "random", "secrets", "time", "logging", "glob",
}  # fmt: skip


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_bgm_modules_do_not_import_production_or_provider_code() -> None:
    violations = [
        f"{rel}: imports {m}"
        for rel in BGM_MODULES
        for m in _imports(REPO / rel)
        if m.startswith(FORBIDDEN_PREFIXES)
    ]
    assert not violations, "\n".join(violations)


def test_pure_bgm_domain_modules_do_no_io_or_clock() -> None:
    violations = [
        f"{rel}: imports {m}"
        for rel in (
            "domain/render/bgm.py",
            "domain/render/loudness.py",
            "contracts/music_render.py",
        )
        for m in _imports(REPO / rel)
        if m.split(".")[0] in DOMAIN_BANNED_STDLIB
    ]
    assert not violations, "\n".join(violations)


def test_the_setting_key_is_read_only_where_the_worker_is_assembled() -> None:
    readers = sorted(
        str(p.relative_to(REPO))
        for layer in ("apps", "workers", "domain", "infrastructure", "contracts")
        for p in (REPO / layer).rglob("*.py")
        if "bgm_enabled" in p.read_text(encoding="utf-8")
    )
    # 定義（Settings）・キー名の唯一の定義（contracts）・worker 組み立ての読み手 1 箇所
    assert readers == [
        "contracts/music_render.py",
        "infrastructure/config.py",
        "workers/render/run_worker.py",
    ]


def test_the_render_worker_loads_no_fal_or_production_module() -> None:
    code = (
        "import sys; sys.path.insert(0, '.');"
        "import workers.render.activities, workers.render.run_worker, workers.render.workflows;"
        "bad=[m for m in sys.modules if m.startswith(("
        "'infrastructure.providers','workers.production','workers.generation'))];"
        "print(','.join(sorted(bad)))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=REPO, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "", done.stdout


def test_render_activity_names_and_io_dataclass_fields_are_unchanged() -> None:
    """Temporal の履歴に残る形。BGM のために足さない・並べ替えない（ADR-0077 §7）。"""
    assert ra.RENDER_ACTIVITY_NAMES == (
        "render_admit",
        "render_final_video",
        "render_mark_ready",
        "render_record_failure",
    )
    fields = {
        cls.__name__: tuple(f.name for f in dataclasses.fields(cls))
        for cls in (
            ra.RenderAdmitRequest,
            ra.RenderAdmitResult,
            ra.RenderMarkReadyRequest,
            ra.RenderMarkReadyResult,
            ra.RenderRecordFailureRequest,
            ra.RenderFailureOutcome,
            ra.RenderFinalVideoRequest,
            ra.RenderFinalVideoResult,
        )
    }
    assert fields == {
        "RenderAdmitRequest": ("episode_id", "workflow_id", "run_id"),
        "RenderAdmitResult": ("admitted", "status", "render_timeout_seconds"),
        "RenderMarkReadyRequest": ("episode_id", "workflow_id", "run_id"),
        "RenderMarkReadyResult": ("status", "owned"),
        "RenderRecordFailureRequest": (
            "episode_id",
            "workflow_id",
            "run_id",
            "failure_class",
            "error_summary",
            "retry_exhausted",
            "job_id",
        ),  # fmt: skip
        "RenderFailureOutcome": ("episode_status", "owned"),
        "RenderFinalVideoRequest": ("episode_id", "workflow_id", "run_id", "render_profile_id"),
        "RenderFinalVideoResult": ("artifact_id", "sha256", "version", "skipped", "job_id"),
    }
