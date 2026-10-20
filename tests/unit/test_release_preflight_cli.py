"""scripts/release-preflight.py の皮（引数 → Request、終了コード）と compose の queue 導出。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from domain import release_preflight as rp
from infrastructure.release.compose_queues import required_queues
from tests.unit.test_release_preflight import SHA, Fake

ROOT = Path(__file__).resolve().parents[2]


def _cli():
    spec = importlib.util.spec_from_file_location(
        "release_preflight_cli", ROOT / "scripts" / "release-preflight.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARGS = [
    "--target-sha",
    SHA,
    "--backup-manifest",
    "backup.json",
    "--replay-evidence",
    "replay.json",
    "--approval-file",
    "approval.json",
]


def test_cli_ready_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli().main(ARGS + ["--json"], collectors=Fake()) == 0
    assert json.loads(capsys.readouterr().out)["overall"] == "READY"


def test_cli_blocked_exits_2_and_unknown_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli().main(ARGS, collectors=Fake(pending=1)) == 2
    assert "OVERALL: BLOCKED" in capsys.readouterr().out
    assert _cli().main(ARGS, collectors=Fake(ci=rp.CollectorUnavailable("gh"))) == 3
    assert "OVERALL: UNKNOWN" in capsys.readouterr().out


def test_cli_blocked_beats_unknown() -> None:
    assert _cli().main(ARGS, collectors=Fake(pending=1, ci=RuntimeError("x"))) == 2


def test_required_queues_come_from_the_compose_healthchecks() -> None:
    queues = required_queues((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert {"pipeline", "render", "upload", "script", "episode-skeleton"} <= set(queues)
    assert queues["production-image"] == 2700.0
    assert queues["render"] == 120.0
