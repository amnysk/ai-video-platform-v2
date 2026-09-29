"""``scripts/with-maintenance-pause.sh``（ADR-0027）: deploy を maintenance pause で包む。

コマンドが成功したときだけ end で pause を戻す。失敗・中断（SIGTERM 含む）では
**戻さない**（壊れた状態のまま自動生成を再開しない、が唯一の目的）。以前このファイルは
「失敗しても必ず end される」ことを固定していたが、それは deploy 失敗時に自動生成が
再開してしまう実際のバグだった（R3 で修正）。ガード CLI は stub に差し替え、
呼ばれた順序と終了コードだけを見る。
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
WRAPPER = REPO / "scripts" / "with-maintenance-pause.sh"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$2" in
  begin) exit "${STUB_BEGIN_RC:-0}" ;;
  end) exit "${STUB_END_RC:-0}" ;;
esac
"""


@pytest.fixture
def stub(tmp_path: pathlib.Path) -> dict[str, str]:
    script = tmp_path / "guard"
    script.write_text(STUB)
    script.chmod(0o755)
    log = tmp_path / "calls.log"
    return {"SCHEDULE_GUARD": str(script), "STUB_LOG": str(log)}


def _run(
    env_extra: dict[str, str], *command: str, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [str(WRAPPER), "--reason", "deploy-workers", "--ttl", "45m", "--", *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _calls(env: dict[str, str]) -> list[str]:
    path = pathlib.Path(env["STUB_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def test_success_pauses_runs_and_releases_in_order(stub: dict[str, str], tmp_path) -> None:
    marker = tmp_path / "ran"
    result = _run(stub, "touch", str(marker))
    assert result.returncode == 0
    assert marker.exists()
    assert _calls(stub) == [
        "maintenance begin --reason deploy-workers --ttl 45m",
        "maintenance end",
    ]


def test_a_failing_deploy_does_not_release_the_pause(stub: dict[str, str]) -> None:
    """R3: 壊れた状態のまま自動生成を再開しない。end は呼ばれず、pause は印付きで残る。"""
    result = _run(stub, "bash", "-c", "exit 7")
    assert result.returncode == 7
    assert _calls(stub) == ["maintenance begin --reason deploy-workers --ttl 45m"]
    assert "NOT unpausing" in result.stderr
    assert "reconcile" in result.stderr


def test_an_operator_pause_is_left_alone(stub: dict[str, str]) -> None:
    """Test C（wrapper 側）: emergency pause なら、コマンドは動くが end は呼ばない。"""
    result = _run({**stub, "STUB_BEGIN_RC": "3"}, "true")
    assert result.returncode == 0
    assert _calls(stub) == ["maintenance begin --reason deploy-workers --ttl 45m"]
    assert "NOT unpausing" in result.stderr


def test_when_begin_fails_the_command_is_not_run(stub: dict[str, str], tmp_path) -> None:
    marker = tmp_path / "ran"
    result = _run({**stub, "STUB_BEGIN_RC": "1"}, "touch", str(marker))
    assert result.returncode == 1
    assert not marker.exists()
    assert all("end" not in c.split() for c in _calls(stub))


def test_a_failed_release_fails_the_whole_run_even_if_the_command_succeeded(
    stub: dict[str, str],
) -> None:
    result = _run({**stub, "STUB_END_RC": "1"}, "true")
    assert result.returncode == 1
    assert "may still be paused" in result.stderr


def test_termination_during_the_command_does_not_release(stub: dict[str, str]) -> None:
    """R3: SIGTERM は「失敗」と同じ扱い。中断された deploy のまま自動生成を再開しない。"""
    env = {**os.environ, **stub}
    proc = subprocess.Popen(
        [str(WRAPPER), "--reason", "deploy-workers", "--", "sleep", "30"],
        env=env,
        start_new_session=True,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline and not _calls(stub):
        time.sleep(0.05)
    time.sleep(0.3)  # sleep が始まるのを待つ
    os.killpg(proc.pid, signal.SIGTERM)
    _, stderr = proc.communicate(timeout=10)
    assert _calls(stub) == ["maintenance begin --reason deploy-workers"]
    assert proc.returncode != 0
    assert "NOT unpausing" in stderr


def test_missing_arguments_are_a_usage_error() -> None:
    result = subprocess.run([str(WRAPPER)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
