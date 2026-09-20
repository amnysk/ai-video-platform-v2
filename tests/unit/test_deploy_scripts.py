"""scripts/deploy-workers.sh と scripts/workers-versions.sh の振る舞い検査。

本物の docker / git は呼ばず、PATH の先頭に置いた shim（呼び出しを記録し、状態を固定値で返す）で
実行する。デプロイ手順の「順序」「フック」「失敗時に必ず後始末が走る」は
実 Docker では試せない（本番を触る）ので、ここで固定する。根拠は docs/testing/worker-versions.md。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy-workers.sh"
VERSIONS = ROOT / "scripts" / "workers-versions.sh"

DOCKER_SHIM = r"""#!/usr/bin/env bash
# 呼び出しを記録し、状態は $SHIM_DIR の固定ファイルから返す。format で目的を見分ける。
echo "docker $*" >> "$SHIM_DIR/log"
case "$*" in
  *"compose"*" ps "*)
    svc="${@: -1}"
    f="$SHIM_DIR/ids_$svc"; [ "$svc" = "-q" ] && f="$SHIM_DIR/all_ids"
    cat "$f" 2>/dev/null
    exit 0 ;;
  *"compose"*" build "*) exit "$(cat "$SHIM_DIR/build_rc" 2>/dev/null || echo 0)" ;;
  *"compose"*" up "*)    exit "$(cat "$SHIM_DIR/up_rc" 2>/dev/null || echo 0)" ;;
  "image inspect"*) cat "$SHIM_DIR/tag_$(basename "${@: -1}" | tr ':' '_')" ;;
  "inspect"*)
    cid="${@: -1}"
    case "$*" in
      *State.Status*) cat "$SHIM_DIR/state_$cid" ;;
      *) cat "$SHIM_DIR/ver_$cid" ;;
    esac ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
"""

GIT_SHIM = r"""#!/usr/bin/env bash
case "$*" in
  "rev-parse HEAD") echo "${SHIM_REV:-1111111111111111111111111111111111111111}" ;;
  "status --porcelain"*) printf '%s' "${SHIM_DIRTY:-}" ;;
  *) echo "unexpected git call: $*" >&2; exit 99 ;;
esac
"""

REV = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"


class Shim:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "shim"
        self.bin = tmp_path / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        for name, body in (("docker", DOCKER_SHIM), ("git", GIT_SHIM)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.hook_log = tmp_path / "hooks"

    def put(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, encoding="utf-8")

    def containers(self, rows: dict[str, tuple[str, str, str]], tag_ids: dict[str, str]) -> None:
        """rows: container id -> (service, image id, revision)。tag_ids: image tag -> id。"""
        self.put("all_ids", "\n".join(rows) + "\n")
        for cid, (service, image_id, rev) in rows.items():
            image = "avp2-app:local" if service in {"api", "migrate"} else "avp2-worker:local"
            self.put(f"ver_{cid}", f"{service} {image} sha256:{image_id} {rev}\n")
        for tag, image_id in tag_ids.items():
            self.put(f"tag_{tag.replace(':', '_')}", f"sha256:{image_id}\n")

    def state(
        self, cid: str, service: str, status: str, health: str, exit_code: int, restart: str
    ) -> None:
        self.put(f"state_{cid}", f"{service} {status} {health or '-'} {exit_code} {restart}\n")
        self.put(f"ids_{service}", f"{cid}\n")

    def run(self, script: Path, *args: str, env: dict[str, str] | None = None):
        full = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "SHIM_DIR": str(self.dir),
            "POLL_INTERVAL": "0.05",
            "HEALTH_TIMEOUT": "1",
            **(env or {}),
        }
        return subprocess.run(
            ["bash", str(script), *args], capture_output=True, text=True, env=full, timeout=60
        )

    @property
    def docker_calls(self) -> list[str]:
        path = self.dir / "log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
def shim(tmp_path: Path) -> Shim:
    return Shim(tmp_path)


APP = {
    "migrate": "m1",
    "api": "a1",
    "dummy-worker": "d1",
    "script-worker": "s1",
    "storyboard-worker": "sb1",
    "production-worker": "p1",
    "production-image-worker": "pi1",
    "production-voice-worker": "pv1",
    "production-video-worker": "pvd1",
    "render-worker": "r1",
    "upload-worker": "u1",
    "pipeline-worker": "pl1",
}
INFRA = {"postgres": "pg1", "temporal": "t1", "minio": "mn1"}


def _healthy_stack(shim: Shim, *, revision: str = REV) -> None:
    for service, cid in INFRA.items():
        shim.state(cid, service, "running", "healthy", 0, "unless-stopped")
    for service, cid in APP.items():
        if service == "migrate":
            shim.state(cid, service, "exited", "", 0, "no")
        elif service == "api":
            shim.state(cid, service, "running", "", 0, "unless-stopped")
        else:
            shim.state(cid, service, "running", "healthy", 0, "unless-stopped")
    shim.containers(
        {
            cid: (service, "aaaa" if service in {"api", "migrate"} else "bbbb", revision)
            for service, cid in APP.items()
        },
        {"avp2-app:local": "aaaa", "avp2-worker:local": "bbbb"},
    )


# ---------------------------------------------------------------- workers-versions.sh


def test_versions_ok_when_all_containers_share_current_image_and_revision(shim: Shim) -> None:
    _healthy_stack(shim)
    result = shim.run(VERSIONS)
    assert result.returncode == 0, result.stderr
    assert REV in result.stdout


def test_versions_fail_on_stale_container(shim: Shim) -> None:
    """コンテナの image id が現在のタグと違う = 作り直されていない（2026-09-20 の事故の形）。"""
    _healthy_stack(shim)
    shim.containers(
        {
            "r1": ("render-worker", "old0", REV),
            "s1": ("script-worker", "bbbb", REV),
        },
        {"avp2-app:local": "aaaa", "avp2-worker:local": "bbbb"},
    )
    result = shim.run(VERSIONS)
    assert result.returncode == 1
    assert "STALE" in result.stdout


def test_versions_fail_on_mixed_revisions(shim: Shim) -> None:
    _healthy_stack(shim)
    shim.containers(
        {
            "r1": ("render-worker", "bbbb", REV),
            "s1": ("script-worker", "bbbb", OTHER),
        },
        {"avp2-app:local": "aaaa", "avp2-worker:local": "bbbb"},
    )
    result = shim.run(VERSIONS)
    assert result.returncode == 1
    assert "revision" in result.stderr


@pytest.mark.parametrize("label", ["-", "unknown"])
def test_versions_fail_when_revision_is_unidentifiable(shim: Shim, label: str) -> None:
    _healthy_stack(shim, revision=label)
    result = shim.run(VERSIONS)
    assert result.returncode == 1


def test_versions_fail_when_revision_differs_from_expected(shim: Shim) -> None:
    _healthy_stack(shim)
    result = shim.run(VERSIONS, env={"EXPECTED_REVISION": OTHER})
    assert result.returncode == 1
    assert OTHER in result.stderr


# ---------------------------------------------------------------- deploy-workers.sh


def _deploy(
    shim: Shim, *, pre: str = "true", post: str | None = None, env: dict[str, str] | None = None
):
    hook = shim.hook_log
    post_cmd = post or f'echo "post result=$DEPLOY_RESULT rev=$DEPLOY_REVISION" >> {hook}'
    return shim.run(
        DEPLOY,
        env={
            "PRE_DEPLOY_CMD": f"echo pre >> {hook}; {pre}",
            "POST_DEPLOY_CMD": post_cmd,
            **(env or {}),
        },
    )


def test_deploy_success_runs_hooks_in_order_and_recreates_everything(shim: Shim) -> None:
    _healthy_stack(shim)
    result = _deploy(shim)
    assert result.returncode == 0, result.stdout + result.stderr
    assert shim.hook_log.read_text().splitlines() == ["pre", f"post result=success rev={REV}"]

    calls = shim.docker_calls
    builds = [c for c in calls if " build " in c]
    assert len(builds) == 2  # app と worker を1回ずつ
    assert all(f"GIT_REVISION={REV}" in c for c in builds)
    up = [c for c in calls if " up " in c]
    assert len(up) == 2  # migrate を先に、残りをあと
    assert "migrate" in up[0] and "api" not in up[0]
    assert "--no-deps" in up[0] and "--no-build" in up[0]
    for service in APP:
        assert any(service in c for c in up), service
    assert not any("postgres" in c or "minio" in c for c in up)
    first_build = min(i for i, c in enumerate(calls) if " build " in c)
    first_up = min(i for i, c in enumerate(calls) if " up " in c)
    assert first_build < first_up


def test_deploy_refuses_dirty_tree_before_touching_anything(shim: Shim) -> None:
    _healthy_stack(shim)
    result = _deploy(shim, env={"SHIM_DIRTY": " M workers/render/activities.py\n"})
    assert result.returncode != 0
    assert "dirty" in result.stderr
    assert not shim.hook_log.exists()  # フックも走らない（何も変えていない）
    assert not any(" build " in c or " up " in c for c in shim.docker_calls)


def test_deploy_allows_dirty_tree_only_with_explicit_override_and_marks_revision(
    shim: Shim,
) -> None:
    _healthy_stack(shim, revision=f"{REV}-dirty")
    result = _deploy(shim, env={"SHIM_DIRTY": " M x\n", "ALLOW_DIRTY": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(f"GIT_REVISION={REV}-dirty" in c for c in shim.docker_calls if " build " in c)


def test_failed_build_stops_before_recreate_but_post_hook_still_runs(shim: Shim) -> None:
    """途中で失敗しても後始末（POST）は必ず走る。schedule を止めたまま放置しないための固定点。"""
    _healthy_stack(shim)
    shim.put("build_rc", "1")
    result = _deploy(shim)
    assert result.returncode != 0
    assert not any(" up " in c for c in shim.docker_calls)
    assert shim.hook_log.read_text().splitlines() == ["pre", f"post result=failure rev={REV}"]


def test_failed_pre_hook_aborts_before_build_and_post_hook_runs(shim: Shim) -> None:
    _healthy_stack(shim)
    result = _deploy(shim, pre="exit 7")
    assert result.returncode != 0
    assert not any(" build " in c for c in shim.docker_calls)
    assert shim.hook_log.read_text().splitlines() == ["pre", f"post result=failure rev={REV}"]


def test_unhealthy_worker_fails_the_deploy_and_post_hook_reports_failure(shim: Shim) -> None:
    _healthy_stack(shim)
    shim.state("r1", "render-worker", "running", "starting", 0, "unless-stopped")
    result = _deploy(shim)
    assert result.returncode != 0
    assert "render-worker" in result.stderr
    assert shim.hook_log.read_text().splitlines()[-1] == f"post result=failure rev={REV}"


def test_failed_migrate_stops_before_workers_are_recreated(shim: Shim) -> None:
    _healthy_stack(shim)
    shim.state("m1", "migrate", "exited", "", 1, "no")
    result = _deploy(shim)
    assert result.returncode != 0
    up = [c for c in shim.docker_calls if " up " in c]
    assert len(up) == 1 and "migrate" in up[0]


def test_stale_container_after_recreate_fails_the_deploy(shim: Shim) -> None:
    _healthy_stack(shim)
    shim.containers(
        {"r1": ("render-worker", "old0", REV), "s1": ("script-worker", "bbbb", REV)},
        {"avp2-app:local": "aaaa", "avp2-worker:local": "bbbb"},
    )
    result = _deploy(shim)
    assert result.returncode != 0
    assert "STALE" in result.stdout


def test_unhealthy_infrastructure_is_refused_before_building(shim: Shim) -> None:
    _healthy_stack(shim)
    shim.state("t1", "temporal", "running", "unhealthy", 0, "unless-stopped")
    result = _deploy(shim)
    assert result.returncode != 0
    assert "temporal" in result.stderr
    assert not any(" build " in c for c in shim.docker_calls)
    assert not shim.hook_log.exists()  # 何も変える前に中止するので、フックも走らせない


def test_post_hook_failure_makes_an_otherwise_good_deploy_fail(shim: Shim) -> None:
    """unpause 等の後始末が失敗したのに成功と報告しない。"""
    _healthy_stack(shim)
    result = _deploy(shim, post="exit 3")
    assert result.returncode != 0
    assert "POST_DEPLOY_CMD" in result.stderr
