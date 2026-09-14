"""INV-18: テストとCIから有料API・実投稿へ到達しうる依存を持ち込まない。"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

# Phase 1 では有料provider/YouTubeの実装自体を持たない。
FORBIDDEN_TOKENS = {
    "fal.run": "fal.ai endpoint",
    "fal.ai": "fal.ai",
    "queue.fal": "fal.ai queue",
    "googleapis.com/youtube": "YouTube API",
    "youtube.googleapis.com": "YouTube API",
    "googleapis.com/upload/youtube": "YouTube upload API",
    "oauth2.googleapis.com": "Google OAuth token endpoint",
    "accounts.google.com/o/oauth2": "Google OAuth consent",
    "api.openai.com": "OpenAI",
    "api.anthropic.com": "Anthropic",
}

SEARCHED_DIRS = ["apps", "workers", "domain", "infrastructure", "contracts", "tests"]
SELF = pathlib.Path(__file__).resolve()

#: fal の endpoint を書いてよいのは provider adapter だけ（ADR-0017）。
FAL_TOKENS = frozenset({"fal.run", "fal.ai", "queue.fal"})


#: YouTube / Google OAuth の endpoint を書けるのは YouTube adapter と同意スクリプトだけ（Phase 6）
YOUTUBE_TOKENS = frozenset(
    {
        "googleapis.com/youtube",
        "youtube.googleapis.com",
        "googleapis.com/upload/youtube",
        "oauth2.googleapis.com",
        "accounts.google.com/o/oauth2",
    }
)


def _sanctioned_for(token: str, rel: pathlib.PurePosixPath) -> bool:
    if token in FAL_TOKENS:
        return rel.match("infrastructure/providers/fal_*.py")
    if token in YOUTUBE_TOKENS:
        return (
            rel.match("infrastructure/youtube/*.py") or rel.as_posix() == "scripts/youtube-oauth.py"
        )
    return False


def test_no_live_provider_endpoints_in_the_codebase() -> None:
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            if path.resolve() == SELF:
                continue
            source = path.read_text(encoding="utf-8")
            rel = pathlib.PurePosixPath(path.relative_to(REPO).as_posix())
            for token, label in FORBIDDEN_TOKENS.items():
                if token in source and not _sanctioned_for(token, rel):
                    violations.append(f"{path.relative_to(REPO)}: mentions {label} (INV-18)")
    assert not violations, "\n".join(violations)


def test_fal_tokens_are_sanctioned_only_in_fal_adapters() -> None:
    assert _sanctioned_for(
        "queue.fal", pathlib.PurePosixPath("infrastructure/providers/fal_image.py")
    )
    for rel in (
        "infrastructure/providers/codex_cli.py",
        "workers/production_image/activities.py",
        "tests/unit/test_fal_image.py",
        "domain/production/ports.py",
    ):
        assert not _sanctioned_for("fal.ai", pathlib.PurePosixPath(rel)), rel
    assert not _sanctioned_for(
        "api.openai.com", pathlib.PurePosixPath("infrastructure/providers/fal_image.py")
    )


def test_youtube_tokens_are_sanctioned_only_in_the_youtube_adapter() -> None:
    assert _sanctioned_for(
        "googleapis.com/youtube", pathlib.PurePosixPath("infrastructure/youtube/uploader.py")
    )
    assert _sanctioned_for(
        "oauth2.googleapis.com", pathlib.PurePosixPath("scripts/youtube-oauth.py")
    )
    for rel in (
        "infrastructure/providers/fal_queue.py",
        "workers/upload/activities.py",
        "domain/upload/ports.py",
        "tests/unit/test_youtube_uploader.py",
        "scripts/smoke.sh",
    ):
        assert not _sanctioned_for("googleapis.com/youtube", pathlib.PurePosixPath(rel)), rel


def test_scripts_mention_youtube_endpoints_only_in_the_consent_script() -> None:
    violations: list[str] = []
    for path in sorted((REPO / "scripts").rglob("*")):
        if not path.is_file():
            continue
        rel = pathlib.PurePosixPath(path.relative_to(REPO).as_posix())
        source = path.read_text(encoding="utf-8", errors="ignore")
        for token in YOUTUBE_TOKENS:
            if token in source and not _sanctioned_for(token, rel):
                violations.append(f"{rel}: mentions {token} (INV-18)")
    assert not violations, "\n".join(violations)


YOUTUBE_ADAPTER_PREFIX = "infrastructure.youtube"


def test_youtube_adapter_is_not_imported_by_apps_domain_or_contracts() -> None:
    """API は投稿しない。YouTube adapter を組むのは upload worker だけ（INV-16 / Phase 6）。"""
    violations: list[str] = []
    for layer in ("apps", "domain", "contracts"):
        for path in sorted((REPO / layer).rglob("*.py")):
            if _imports_module(path, YOUTUBE_ADAPTER_PREFIX):
                violations.append(f"{path.relative_to(REPO)}: imports {YOUTUBE_ADAPTER_PREFIX}")
    assert not violations, "\n".join(violations)


def test_domain_upload_port_has_no_http_or_oauth() -> None:
    for path in sorted((REPO / "domain" / "upload").rglob("*.py")):
        for module in ("httpx", "google", "googleapiclient", "requests", "infrastructure"):
            assert not _imports_module(path, module), f"{path.relative_to(REPO)} imports {module}"


# --- live テストの隔離（AGENTS.md §9） ------------------------------------

import ast  # noqa: E402
import re  # noqa: E402
import tomllib  # noqa: E402

LIVE_SWITCH = "AVP_LIVE_CODEX"
CODEX_ADAPTER_MODULE = "infrastructure.providers.codex_cli"


def _pytest_ini_options() -> dict:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["pytest"]["ini_options"]


def test_live_tests_are_excluded_from_the_default_pytest_run() -> None:
    """引数なしの ``pytest`` が本物の Codex を呼ばないこと。"""
    options = _pytest_ini_options()
    markers = " ".join(options.get("markers", []))
    assert "live:" in markers, "live マーカーが pyproject.toml に登録されていない"
    addopts = options.get("addopts", "")
    addopts = " ".join(addopts) if isinstance(addopts, list) else addopts
    assert "not live" in addopts.replace("'", "").replace('"', "")


def test_live_conftest_refuses_to_import_without_the_env_switch() -> None:
    source = (REPO / "tests/live/conftest.py").read_text(encoding="utf-8")
    assert "collect_ignore_glob" in source
    assert LIVE_SWITCH in source


def test_no_ci_workflow_enables_the_live_codex_switch() -> None:
    """CI が live スイッチを **有効化** しないこと。

    ``AVP_LIVE_CODEX: ""`` のように**塞ぐ**記述は違反ではない（むしろ望ましい）。
    禁じるのは真値を入れること、``tests/live`` を走らせること、``-m live`` を使うこと。
    """
    enabling = re.compile(
        rf"""(
            {LIVE_SWITCH}\s*[:=]\s*["']?(?!["']?\s*(?:$|\n))(?!""|'')\S
          | (?:pytest|python\s+-m\s+pytest)[^\n]*tests/live
          | -m\s+["']?live
        )""",
        re.VERBOSE,
    )
    hits: list[str] = []
    workflows = REPO / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if enabling.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()} (INV-18)")
    assert not hits, "\n".join(hits)


def _imports_module(path: pathlib.Path, module: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == module or a.name.startswith(module + ".") for a in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and (node.module == module or node.module.startswith(module + "."))
        ):
            return True
    return False


def test_only_sanctioned_modules_import_the_codex_cli_adapter() -> None:
    """有料/実CLI呼び出しの入口を数えられる場所に限る。"""
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            rel = path.relative_to(REPO)
            if not _imports_module(path, CODEX_ADAPTER_MODULE):
                continue
            sanctioned = (
                (rel.parts[0] == "workers" and rel.name == "run_worker.py")
                or rel.parts[:2] == ("tests", "live")
                or rel.as_posix() == "tests/unit/test_codex_adapter.py"
                or rel.as_posix() == f"{CODEX_ADAPTER_MODULE.replace('.', '/')}.py"
            )
            if not sanctioned:
                violations.append(f"{rel}: imports {CODEX_ADAPTER_MODULE} (INV-18)")
    assert not violations, "\n".join(violations)


STORYBOARD_ADAPTER_MODULE = "infrastructure.providers.openmontage_storyboard"

#: storyboard adapter（外部仕様 + 実CLI）を import してよいファイル。**完全一致**で数える。
STORYBOARD_ADAPTER_IMPORTERS = frozenset(
    {
        "workers/storyboard/run_worker.py",
        "tests/unit/test_openmontage_storyboard_adapter.py",
        "tests/live/test_openmontage_storyboard_live.py",
    }
)


def test_only_sanctioned_modules_import_the_openmontage_storyboard_adapter() -> None:
    """有料/実CLI呼び出しの入口を数えられる場所に限る（INV-18 / ADR-0016）。

    関数内の import も ``ast.walk`` で拾う（run_worker は main() 内で import する）。
    """
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel == f"{STORYBOARD_ADAPTER_MODULE.replace('.', '/')}.py":
                continue
            if _imports_module(path, STORYBOARD_ADAPTER_MODULE) and (
                rel not in STORYBOARD_ADAPTER_IMPORTERS
            ):
                violations.append(f"{rel}: imports {STORYBOARD_ADAPTER_MODULE} (INV-18)")
    assert not violations, "\n".join(violations)


FAL_ADAPTER_PREFIX = "infrastructure.providers.fal_"

#: 有料 fal adapter を import してよいファイル（ADR-0017）。**完全一致**で数える。
FAL_ADAPTER_IMPORTERS = frozenset(
    {
        "workers/production_image/run_worker.py",
        "tests/unit/test_fal_queue.py",
        "tests/unit/test_fal_seedream_image.py",
        "tests/live/test_fal_image_live.py",
        "workers/production_video/run_worker.py",
        "tests/unit/test_fal_storage.py",
        "tests/unit/test_fal_seedance_video.py",
        "tests/live/test_fal_video_live.py",
    }
)


def _imports_fal_adapter(path: pathlib.Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        if any(n.startswith(FAL_ADAPTER_PREFIX) for n in names):
            return True
    return False


def test_only_sanctioned_modules_import_fal_adapters() -> None:
    """有料 provider の入口を数えられる場所に限る（INV-18 / ADR-0017）。"""
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith("infrastructure/providers/fal_"):
                continue
            if _imports_fal_adapter(path) and rel not in FAL_ADAPTER_IMPORTERS:
                violations.append(f"{rel}: imports {FAL_ADAPTER_PREFIX}* (INV-18)")
    assert not violations, "\n".join(violations)
