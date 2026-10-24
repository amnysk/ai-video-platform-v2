"""CI が破壊的テストに渡す DB URL は、隔離された `_test` DB だけ（INV-69 / ADR-0055）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from infrastructure.db.safety import validate_test_database_url

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _envs() -> list[tuple[str, dict[str, Any]]]:
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    found: list[tuple[str, dict[str, Any]]] = []
    for job_name, job in wf["jobs"].items():
        found.append((job_name, job.get("env") or {}))
        for step in job.get("steps", []):
            found.append((job_name, step.get("env") or {}))
    return found


def test_ci_test_database_urls_are_isolated() -> None:
    seen = 0
    for _job, env in _envs():
        url = env.get("TEST_DATABASE_URL")
        if url is None:
            continue
        seen += 1
        validate_test_database_url(url, forbidden_urls=[env.get("DATABASE_URL")])
    assert seen >= 2  # integration と alembic。検査対象が消えたら気づく


def test_ci_integration_steps_always_set_test_database_url() -> None:
    # 破壊的テストを走らせる step に TEST_DATABASE_URL が無いと、テストは skip するだけで
    # 本番形の DATABASE_URL を読めてしまう経路が残る。pytest integration の step は必ず両方持つ
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for job_name in ("integration", "alembic"):
        for step in wf["jobs"][job_name]["steps"]:
            if "pytest tests/integration" in str(step.get("run", "")):
                assert "TEST_DATABASE_URL" in (step.get("env") or {}), (job_name, step)
