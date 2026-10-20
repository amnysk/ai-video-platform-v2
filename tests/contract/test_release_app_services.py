"""preflight が「常駐しているべきサービス」を既存の定義から導く（AGENTS.md §8）。"""

from __future__ import annotations

from pathlib import Path

import yaml

from infrastructure.release.app_services import (
    app_services,
    expected_running_services,
    one_shot_services,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "deploy-workers.sh").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "compose.yaml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)


def test_app_services_come_from_the_deploy_script() -> None:
    assert "migrate" in app_services(SCRIPT) and "api" in app_services(SCRIPT)
    assert len(app_services(SCRIPT)) >= 11


def test_one_shot_services_match_compose_restart_no() -> None:
    expected = {n for n, s in COMPOSE["services"].items() if str(s.get("restart")) == "no"}
    assert expected  # migrate
    assert one_shot_services(COMPOSE_TEXT) == expected


def test_expected_running_excludes_only_one_shots() -> None:
    running = expected_running_services(SCRIPT, COMPOSE_TEXT)
    assert "migrate" not in running
    assert set(running) == set(app_services(SCRIPT)) - one_shot_services(COMPOSE_TEXT)
    assert "api" in running and "pipeline-worker" in running
