"""compose.yaml が Worker 常駐構成の契約を守っていることの検査。

期待する queue はコード上の定数から導く（文字列の二重管理をしない）。
dummy-worker は既存の base イメージのままなので「単一 worker イメージ」検査からは外す。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts.pipeline import PIPELINE_TASK_QUEUE
from contracts.states import (
    PRODUCTION_IMAGE_TASK_QUEUE,
    PRODUCTION_VIDEO_TASK_QUEUE,
    PRODUCTION_VOICE_TASK_QUEUE,
    PRODUCTION_WORKFLOW,
    RENDER_MEDIA_TASK_QUEUE,
    RENDER_TASK_QUEUE,
    STORYBOARD_WORKFLOW,
    UPLOAD_MEDIA_TASK_QUEUE,
    UPLOAD_TASK_QUEUE,
)
from infrastructure.config import Settings
from workers.planning.run_worker import SCRIPT_TASK_QUEUE

ROOT = Path(__file__).resolve().parents[2]

# service -> (module, queues, uses_minio)
WORKERS: dict[str, tuple[str, set[str], bool]] = {
    "dummy-worker": (
        "workers.dummy.run_worker",
        {Settings.model_fields["temporal_task_queue"].default},
        True,
    ),
    "script-worker": ("workers.planning.run_worker", {SCRIPT_TASK_QUEUE}, True),
    "storyboard-worker": ("workers.storyboard.run_worker", {STORYBOARD_WORKFLOW[1]}, True),
    "production-worker": ("workers.production.run_worker", {PRODUCTION_WORKFLOW[1]}, True),
    "production-image-worker": (
        "workers.production_image.run_worker",
        {PRODUCTION_IMAGE_TASK_QUEUE},
        True,
    ),
    "production-voice-worker": (
        "workers.production_voice.run_worker",
        {PRODUCTION_VOICE_TASK_QUEUE},
        True,
    ),
    "production-video-worker": (
        "workers.production_video.run_worker",
        {PRODUCTION_VIDEO_TASK_QUEUE},
        True,
    ),
    "render-worker": (
        "workers.render.run_worker",
        {RENDER_TASK_QUEUE, RENDER_MEDIA_TASK_QUEUE},
        True,
    ),
    "upload-worker": (
        "workers.upload.run_worker",
        {UPLOAD_TASK_QUEUE, UPLOAD_MEDIA_TASK_QUEUE},
        True,
    ),
    "pipeline-worker": ("workers.pipeline.run_worker", {PIPELINE_TASK_QUEUE}, False),
}
NEW_WORKERS = [s for s in WORKERS if s != "dummy-worker"]

# healthcheck で見る queue。Activity 専用 queue は実行枠が埋まる長い処理の間 poll しないため
# （temporalio は空き枠があるときだけ poll する）、状態系 queue を持つ worker はそれだけを見る
HEALTH_QUEUES: dict[str, set[str]] = {
    "render-worker": {RENDER_TASK_QUEUE},
    "upload-worker": {UPLOAD_TASK_QUEUE},
}
# Activity 専用 queue だけの worker は、最長の処理より長い max-age で見る（秒）
ACTIVITY_ONLY_MIN_MAX_AGE: dict[str, int] = {
    "production-image-worker": 45 * 60,
    "production-video-worker": 45 * 60,
    "production-voice-worker": 15 * 60,
}
CORE_INFRA = ["postgres", "minio", "temporal", "temporal-ui", "api", "dummy-worker"]


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    return data["services"]


def _svc(services: dict[str, Any], name: str) -> dict[str, Any]:
    assert name in services, f"compose service {name!r} がない"
    return services[name]


def _env(svc: dict[str, Any]) -> dict[str, str]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _resolved(value: str) -> str:
    """`${VAR:-default}` なら default、リテラルならそのまま。"""
    m = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-([^}]*))?\}", value)
    return (m.group(1) or "") if m else value


def _volumes(svc: dict[str, Any]) -> list[tuple[str, bool]]:
    """(container target, read_only) の一覧。"""
    out: list[tuple[str, bool]] = []
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):
            out.append((v["target"], bool(v.get("read_only"))))
        else:
            parts = v.split(":")
            target = parts[1] if len(parts) > 1 else parts[0]
            out.append((target, len(parts) > 2 and "ro" in parts[2].split(",")))
    return out


def _assert_ro_mount_covers(svc: dict[str, Any], container_path: str) -> None:
    assert container_path, "コンテナ内パスが決まらない"
    matches = [
        (t, ro)
        for t, ro in _volumes(svc)
        if container_path == t or container_path.startswith(t.rstrip("/") + "/")
    ]
    assert matches, f"{container_path} を覆う mount がない"
    assert all(ro for _, ro in matches), f"{container_path} の mount が read-only でない"


def _healthcheck_queues(svc: dict[str, Any]) -> set[str]:
    test = svc["healthcheck"]["test"]
    tokens = (
        test.split() if isinstance(test, str) else [t for part in test for t in str(part).split()]
    )
    assert "infrastructure.temporal.poller_check" in tokens
    return {tokens[i + 1] for i, t in enumerate(tokens) if t == "--queue"}


def _cmd_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    return value.split() if isinstance(value, str) else [str(v) for v in value]


# 1 ------------------------------------------------------------------------
@pytest.mark.parametrize("service", list(WORKERS))
def test_worker_service_exists_and_module_defines_main(services, service) -> None:
    module, queues, _ = WORKERS[service]
    assert queues and all(queues)
    path = ROOT / (module.replace(".", "/") + ".py")
    assert path.is_file()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main"
        for n in tree.body
    ), f"{module} に main がない"
    _svc(services, service)


# 2 ------------------------------------------------------------------------
@pytest.mark.parametrize("service", list(WORKERS))
def test_worker_command_and_healthcheck(services, service) -> None:
    module, queues, _ = WORKERS[service]
    svc = _svc(services, service)
    assert _cmd_tokens(svc.get("command")) == [
        "python",
        "-m",
        "infrastructure.runtime.worker_entry",
        module,
    ]
    assert _healthcheck_queues(svc) == HEALTH_QUEUES.get(service, queues)


# 3 ------------------------------------------------------------------------
@pytest.mark.parametrize("service", CORE_INFRA + NEW_WORKERS)
def test_restart_unless_stopped(services, service) -> None:
    assert _svc(services, service).get("restart") == "unless-stopped"


def test_migrate_does_not_restart(services) -> None:
    assert str(_svc(services, "migrate").get("restart")) == "no"


# 4 ------------------------------------------------------------------------
@pytest.mark.parametrize("service", list(WORKERS))
def test_worker_depends_on(services, service) -> None:
    _, _, uses_minio = WORKERS[service]
    deps = _svc(services, service).get("depends_on") or {}
    assert isinstance(deps, dict)
    assert deps.get("postgres", {}).get("condition") == "service_healthy"
    assert deps.get("migrate", {}).get("condition") == "service_completed_successfully"
    assert deps.get("temporal", {}).get("condition") == "service_healthy"
    if uses_minio:
        assert deps.get("minio", {}).get("condition") == "service_healthy"
    else:
        assert "minio" not in deps


# 5 ------------------------------------------------------------------------
@pytest.mark.parametrize("service", ["temporal", "postgres", "minio"])
def test_infra_healthchecks(services, service) -> None:
    assert _svc(services, service).get("healthcheck", {}).get("test")


# 6 ------------------------------------------------------------------------
def test_no_duplicate_workers_or_queues(services) -> None:
    module_owner: dict[str, list[str]] = {}
    queue_owner: dict[str, list[str]] = {}
    for name, svc in services.items():
        cmd = _cmd_tokens(svc.get("command")) + _cmd_tokens(svc.get("entrypoint"))
        for m in (w[0] for w in WORKERS.values()):
            if m in cmd:
                module_owner.setdefault(m, []).append(name)
        if "infrastructure.temporal.poller_check" in str(
            (svc.get("healthcheck") or {}).get("test")
        ):
            for q in _healthcheck_queues(svc):
                queue_owner.setdefault(q, []).append(name)
    for service, (module, queues, _) in WORKERS.items():
        assert module_owner.get(module) == [service]
        # media queue は module の一意性で担保する（healthcheck は状態系 queue だけを見る）
        for q in HEALTH_QUEUES.get(service, queues):
            assert queue_owner.get(q) == [service]


def test_new_workers_share_one_image(services) -> None:
    images = set()
    for service in NEW_WORKERS:
        svc = _svc(services, service)
        build = svc.get("build")
        assert isinstance(build, dict) and build.get("target") == "worker"
        images.add((svc.get("image"), build.get("context")))
    assert len(images) == 1 and next(iter(images))[0]


# 7 ------------------------------------------------------------------------
SECRETISH = re.compile(r"(SECRET|KEY|TOKEN|PASSWORD|PWD)")
ALLOWED_DEFAULTS = {"", "change-me", "minioadmin"}


def test_secrets_are_interpolated_not_inline(services) -> None:
    for name, svc in services.items():
        for key, value in _env(svc).items():
            if not SECRETISH.search(key) or key.endswith("_PATH"):
                continue
            m = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-([^}]*))?\}", value)
            assert m, f"{name}.{key} が補間でない"
            assert (m.group(1) or "") in ALLOWED_DEFAULTS, f"{name}.{key} の既定値が秘密っぽい"


def test_refresh_token_only_via_path_and_ro_volume(services) -> None:
    for name, svc in services.items():
        for key in _env(svc):
            if "REFRESH_TOKEN" in key:
                assert key.endswith("_PATH"), f"{name}.{key}"
    upload = _svc(services, "upload-worker")
    token_path = _resolved(_env(upload).get("YOUTUBE_REFRESH_TOKEN_PATH", ""))
    _assert_ro_mount_covers(upload, token_path)


def test_no_service_registers_schedule(services) -> None:
    for name, svc in services.items():
        text = " ".join(_cmd_tokens(svc.get("command")) + _cmd_tokens(svc.get("entrypoint")))
        assert "ensure-daily-schedule" not in text and "ensure_daily_schedule" not in text, name


# 8 ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("prefix", "owners"),
    [
        ("FAL_KEY", {"production-image-worker", "production-video-worker"}),
        ("YOUTUBE_", {"upload-worker"}),
        ("CODEX_", {"script-worker", "storyboard-worker"}),
    ],
)
def test_per_worker_minimal_env(services, prefix, owners) -> None:
    having = {
        name for name, svc in services.items() if any(k.startswith(prefix) for k in _env(svc))
    }
    assert having == owners


# 9 ------------------------------------------------------------------------
def test_dockerfile_worker_stage_and_piper_version() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM\s+\S+\s+AS\s+worker\s*$", dockerfile, re.M | re.I)
    m = re.search(r"^ARG\s+PIPER_TTS_VERSION=(\S+)", dockerfile, re.M)
    assert m, "PIPER_TTS_VERSION ARG がない"
    script = (ROOT / "scripts/setup-piper.sh").read_text(encoding="utf-8")
    s = re.search(r'^PIPER_TTS_VERSION="?([^"\s]+)"?', script, re.M)
    assert s and m.group(1).strip('"') == s.group(1)
    base = re.split(r"^FROM\s", dockerfile.split("AS base", 1)[1], flags=re.M)[0]
    assert "pip install -c constraints.txt ." in base


# 10 -----------------------------------------------------------------------
def test_openmontage_mount_read_only(services) -> None:
    svc = _svc(services, "storyboard-worker")
    _assert_ro_mount_covers(svc, _resolved(_env(svc).get("OPENMONTAGE_REPO_PATH", "")))


def test_ffmpeg_mount_read_only(services) -> None:
    svc = _svc(services, "render-worker")
    ffmpeg = _resolved(_env(svc).get("RENDER_FFMPEG_PATH", ""))
    _assert_ro_mount_covers(svc, ffmpeg)


def test_env_example_enables_core_profile_for_plain_up() -> None:
    """素の ``docker compose up -d`` で Worker 群が起動するよう core profile を既定にする。"""
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "COMPOSE_PROFILES=core" in [line.strip() for line in env_example]


def _healthcheck_tokens(svc: dict[str, Any]) -> list[str]:
    test = svc["healthcheck"]["test"]
    return (
        test.split() if isinstance(test, str) else [t for part in test for t in str(part).split()]
    )


@pytest.mark.parametrize("service", list(ACTIVITY_ONLY_MIN_MAX_AGE))
def test_activity_only_workers_tolerate_long_activities(services, service) -> None:
    """長い Activity の実行中（枠が埋まり poll しない）に unhealthy と誤判定しない。"""
    tokens = _healthcheck_tokens(_svc(services, service))
    assert "--max-age-seconds" in tokens
    age = int(tokens[tokens.index("--max-age-seconds") + 1])
    assert age >= ACTIVITY_ONLY_MIN_MAX_AGE[service]


def test_temporal_healthcheck_matches_serving_exactly(services) -> None:
    """``grep -q SERVING`` は ``NOT_SERVING`` にも一致する。行全体で一致させる。"""
    test = _svc(services, "temporal")["healthcheck"]["test"]
    text = test if isinstance(test, str) else " ".join(str(t) for t in test)
    assert "grep -qx SERVING" in text


def test_pipeline_worker_does_not_receive_minio_credentials(services) -> None:
    env = _env(_svc(services, "pipeline-worker"))
    assert not [k for k in env if k.startswith("MINIO_")]


@pytest.mark.parametrize("service", NEW_WORKERS)
def test_workers_drop_capabilities_and_forbid_privilege_gain(services, service) -> None:
    svc = _svc(services, service)
    assert [str(c).upper() for c in svc.get("cap_drop") or []] == ["ALL"]
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])


def test_make_refuses_root_uid_on_rootful_docker() -> None:
    """rootless 用の AVP_UID=0 を rootful Docker で使うと本物の root になる。起動前に止める。"""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "check-docker-uid:" in makefile
    assert "rootless" in makefile
    for target in ("up:", "workers-up:"):
        line = next(ln for ln in makefile.splitlines() if ln.startswith(target))
        assert "check-docker-uid" in line


CODEX_WORKERS = {"script-worker", "storyboard-worker"}


@pytest.mark.parametrize("service", NEW_WORKERS)
def test_only_codex_workers_regain_setfcap(services, service) -> None:
    """Codex の sandbox（bwrap）は uid 0 の map に CAP_SETFCAP が要る（実測。他の cap は不要）。"""
    svc = _svc(services, service)
    expected = ["SETFCAP"] if service in CODEX_WORKERS else []
    assert [str(c).upper() for c in svc.get("cap_add") or []] == expected
    opts = svc.get("security_opt") or []
    assert "no-new-privileges:true" in opts
    assert ("seccomp=unconfined" in opts) == (service in CODEX_WORKERS)
