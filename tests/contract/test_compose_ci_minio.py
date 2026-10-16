"""CI の MinIO は pull できる digest 固定の image を使う（ADR-0046 の契約）。

minio/minio は Docker Hub から匿名 pull できなくなった（2026-09 実測）。本番の compose.yaml は
触らず、CI だけ compose.ci.yaml で image を差し替える。この検査が守るもの:
  - override が digest 固定（latest/タグだけでは再現しない）
  - 非 root image が root 所有の使い捨て data dir に書けるよう user が明示されている
  - integration job が実際にその override を読む（書いただけで未使用、を防ぐ）
  - 本番の compose.yaml は差し替えていない
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def test_ci_override_pins_minio_by_digest() -> None:
    minio = _load("compose.ci.yaml")["services"]["minio"]
    assert re.fullmatch(r"cgr\.dev/chainguard/minio@sha256:[0-9a-f]{64}", minio["image"])


def test_ci_override_runs_as_root_for_disposable_data_dir() -> None:
    assert str(_load("compose.ci.yaml")["services"]["minio"]["user"]) == "0"


def test_integration_job_loads_the_override() -> None:
    wf = _load(".github/workflows/ci.yml")
    env = wf["jobs"]["integration"]["env"]
    assert "compose.ci.yaml" in env["COMPOSE_FILE"]
    assert env["COMPOSE_FILE"].startswith("compose.yaml")


def test_production_compose_is_not_switched() -> None:
    base = _load("compose.yaml")["services"]["minio"]
    assert base["image"].startswith("minio/minio")
    assert "/mnt/minio-hdd/minio-data:/data" in base["volumes"]


def test_workflow_shell_fails_on_pipe_errors() -> None:
    """`pytest ... | tee log` の終了コードは tee のものになり、pytest の失敗が緑になる。

    既定 shell は `bash -e {0}`（pipefail なし）。`shell: bash` は `-eo pipefail`。
    """
    wf = _load(".github/workflows/ci.yml")
    assert wf["defaults"]["run"]["shell"] == "bash"


def test_workflow_token_is_read_only() -> None:
    assert _load(".github/workflows/ci.yml")["permissions"] == {"contents": "read"}


def test_integration_pytest_log_is_uploaded() -> None:
    wf = _load(".github/workflows/ci.yml")
    paths = [
        s["with"]["path"]
        for s in wf["jobs"]["integration"]["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert any("pytest-integration.log" in p for p in paths)
