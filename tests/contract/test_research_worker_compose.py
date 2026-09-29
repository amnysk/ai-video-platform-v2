"""research-worker の compose 構成（ADR-0037 §8.5）。

- 環境は DB・MinIO・Temporal（``x-app-env``）と ``RESEARCH_PROVIDER`` だけ。``YOUTUBE_*`` /
  ``CODEX_*`` / ``FAL_KEY`` を持たない（実 Provider・有料 API に届く資格情報を渡さない）
- ``RESEARCH_PROVIDER`` の既定は ``none``（fail-closed）。api（受け付けの門）と research-worker
  （実行の門）が**同じ変数**を同じ既定で読む
- Codex の sandbox 例外（cap_add / seccomp）を持たない

一般の Worker の契約（command・healthcheck の queue・restart・depends_on・単一イメージ）は
``tests/contract/test_compose_workers.py`` の ``WORKERS`` が検査する。
理由は docs/testing/research-worker-rationale.md。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts.research import DEFAULT_RESEARCH_PROVIDER

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_ENV = "RESEARCH_PROVIDER"
EXPECTED_PROVIDER = "${RESEARCH_PROVIDER:-" + DEFAULT_RESEARCH_PROVIDER + "}"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def _env(svc: dict[str, Any]) -> dict[str, str]:
    env = svc.get("environment") or {}
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def test_the_default_provider_is_none() -> None:
    assert DEFAULT_RESEARCH_PROVIDER == "none"


def test_the_research_worker_env_is_app_env_plus_the_provider_only(compose) -> None:
    services = compose["services"]
    worker = _env(services["research-worker"])
    app_env = {str(k) for k in compose["x-app-env"]}
    assert set(worker) == app_env | {PROVIDER_ENV}
    assert worker[PROVIDER_ENV] == EXPECTED_PROVIDER
    for prefix in ("YOUTUBE_", "CODEX_", "FAL_KEY", "OPENAI", "ANTHROPIC", "GOOGLE"):
        assert not [k for k in worker if k.startswith(prefix)], prefix


def test_the_api_reads_the_same_provider_setting(compose) -> None:
    api = _env(compose["services"]["api"])
    assert api[PROVIDER_ENV] == EXPECTED_PROVIDER


def test_the_provider_is_only_given_to_the_api_and_the_research_worker(compose) -> None:
    having = {name for name, svc in compose["services"].items() if PROVIDER_ENV in _env(svc)}
    assert having == {"api", "research-worker"}


def test_the_research_worker_has_no_codex_sandbox_exceptions(compose) -> None:
    svc = compose["services"]["research-worker"]
    assert not svc.get("cap_add")
    assert "seccomp=unconfined" not in (svc.get("security_opt") or [])
    assert not svc.get("volumes")


def test_env_example_documents_the_fail_closed_default() -> None:
    lines = [
        line.strip() for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    ]
    assert f"{PROVIDER_ENV}={DEFAULT_RESEARCH_PROVIDER}" in lines
