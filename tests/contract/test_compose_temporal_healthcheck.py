"""Temporal コンテナの init と healthcheck が zombie を溜めないことの検査（ADR-0052）。

2026-10-21 の実測: `temporal-server` が PID 1 で子を wait しないため、healthcheck の
`temporal ... | grep` が timeout で孤児になるたびに zombie が2つ溜まり、17,934 個で cgroup の
pids.max(17,968) に達して healthcheck 自体が fork できなくなった。各テストの理由:
docs/testing/temporal-healthcheck-rationale.md。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ["compose.yaml", "compose.staging.yaml"]

# 閾値は緩めない（ADR-0052）。これを超える値への変更は、この定数ごと ADR で変える。
MAX_INTERVAL_S = 10
MAX_TIMEOUT_S = 5
MAX_RETRIES = 30


def _temporal(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    return doc["services"]["temporal"]


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m)?", str(value))
    assert match, value
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def _command(name: str) -> str:
    test = _temporal(name)["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL" and len(test) == 2
    return test[1].replace("$$", "$")  # compose の `$$` は shell に `$` として渡る


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_temporal_runs_under_an_init_that_reaps_orphans(name: str) -> None:
    assert _temporal(name).get("init") is True


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_temporal_healthcheck_has_no_pipeline_and_bounds_the_cli(name: str) -> None:
    cmd = _command(name)
    assert "|" not in cmd, "パイプは timeout で孤児を作る"
    assert re.search(r"timeout -s KILL (\d+) temporal operator cluster health", cmd)
    inner = int(re.search(r"timeout -s KILL (\d+)", cmd).group(1))  # type: ignore[union-attr]
    assert inner < _seconds(_temporal(name)["healthcheck"]["timeout"]), (
        "CLI の timeout が docker の timeout より長いと docker に殺されて孤児になる"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_temporal_healthcheck_thresholds_are_not_loosened(name: str) -> None:
    hc = _temporal(name)["healthcheck"]
    assert _seconds(hc["interval"]) <= MAX_INTERVAL_S
    assert _seconds(hc["timeout"]) <= MAX_TIMEOUT_S
    assert int(hc["retries"]) <= MAX_RETRIES


def test_prod_and_staging_share_the_same_temporal_health_definition() -> None:
    prod, stg = (_temporal(n) for n in COMPOSE_FILES)
    assert prod["healthcheck"] == stg["healthcheck"]
    assert prod["init"] == stg["init"]


def _run_check(tmp_path: Path, name: str, fake_body: str) -> tuple[int, float]:
    """偽の `temporal` を PATH 先頭に置き、本物の healthcheck コマンドを sh で実行する。"""
    fake = tmp_path / "temporal"
    fake.write_text("#!/bin/sh\n" + fake_body + "\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    started = time.monotonic()
    done = subprocess.run(["sh", "-c", _command(name)], env=env, capture_output=True, timeout=20)
    return done.returncode, time.monotonic() - started


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_temporal_healthcheck_passes_only_on_exact_serving(tmp_path: Path, name: str) -> None:
    assert _run_check(tmp_path, name, "echo SERVING")[0] == 0


@pytest.mark.parametrize(
    "body",
    ["echo NOT_SERVING", "echo SERVING_NOT", "echo; exit 0", "echo 'failed' >&2; exit 1", "exit 0"],
)
def test_temporal_healthcheck_fails_when_not_serving(tmp_path: Path, body: str) -> None:
    for name in COMPOSE_FILES:
        assert _run_check(tmp_path, name, body)[0] != 0


def test_temporal_healthcheck_fails_within_docker_timeout_when_server_hangs(tmp_path: Path) -> None:
    """サーバが固まって CLI が戻らない状況（2026-10-21 の再現条件）。"""
    for name in COMPOSE_FILES:
        code, elapsed = _run_check(tmp_path, name, "exec sleep 30")
        assert code != 0
        assert elapsed < _seconds(_temporal(name)["healthcheck"]["timeout"])
