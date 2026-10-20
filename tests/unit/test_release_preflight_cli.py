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
    "--approved-report",
    "report.json",
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


def test_argparse_errors_exit_3_not_2(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse 既定の終了コード 2 は BLOCKED と同じ。使い方の誤りは UNKNOWN(3) に寄せる。"""
    assert _cli().main([], collectors=Fake()) == 3
    assert _cli().main(ARGS + ["--no-such-flag"], collectors=Fake()) == 3
    assert _cli().main(ARGS + ["--evidence-max-age-hours", "abc"], collectors=Fake()) == 3
    capsys.readouterr()


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli().main(["--help"], collectors=Fake()) == 0
    capsys.readouterr()


def test_unexpected_exception_exits_3_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli()

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "run_preflight", boom)
    assert cli.main(ARGS, collectors=Fake()) == 3
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "secret detail" not in captured.err and "Traceback" not in captured.err


def test_new_flags_reach_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    seen: list[rp.Request] = []

    def spy(req: rp.Request, c: object, now: object) -> rp.Report:
        seen.append(req)
        return rp.Report(req.target_sha, "pre-deploy", (), (), rp.Overall.READY)

    monkeypatch.setattr(cli, "run_preflight", spy)
    extra = ["--rollback", "--approved-report", "r.json", "--evidence-max-age-hours", "10"]
    assert cli.main(ARGS + extra, collectors=Fake()) == 0
    req = seen[0]
    assert req.rollback and req.approved_report == "r.json" and req.evidence_max_age_hours == 10
