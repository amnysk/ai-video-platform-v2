"""scripts/monitor-watch.py（dead-man's switch）と host-process-monitor の heartbeat / 警報統合。

理由は docs/testing/monitor-watch-rationale.md。偽の /proc と tmp の state dir だけを使う。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str, file: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def watch() -> ModuleType:
    return _load("avp_monitor_watch", "monitor-watch.py")


@pytest.fixture(scope="module")
def mon() -> ModuleType:
    return _load("avp_host_process_monitor_integration", "host-process-monitor.py")


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    return _load("avp_opensearch_guard_for_watch", "opensearch-guard.py")


def _hb(d: Path, name: str, at: float) -> None:
    (d / f"heartbeat-{name}.json").write_text(
        json.dumps({"component": name, "at": at, "level": "ok"})
    )


def _alerts(d: Path, source: str) -> dict[str, dict[str, object]]:
    p = d / "alerts-current.json"
    return json.loads(p.read_text()).get(source, {}) if p.exists() else {}


def test_fresh_heartbeats_raise_nothing_and_the_watcher_writes_its_own(
    watch: ModuleType, tmp_path: Path
) -> None:
    _hb(tmp_path, "opensearch-guard", 990.0)
    _hb(tmp_path, "host-process-monitor", 990.0)
    assert watch.main(["--state-dir", str(tmp_path)], now=1000.0) == 0
    assert _alerts(tmp_path, "monitor-watch") == {}
    assert json.loads((tmp_path / "heartbeat-monitor-watch.json").read_text())["at"] == 1000.0


def test_a_stopped_guard_is_flagged_while_the_other_monitor_stays_quiet(
    watch: ModuleType, tmp_path: Path
) -> None:
    """OpenSearch が落ちて guard 自身も止まった状況: 独立した watcher だけが気付ける。"""
    _hb(tmp_path, "opensearch-guard", 1000.0)
    _hb(tmp_path, "host-process-monitor", 1000.0 + 3000)
    code = watch.main(["--state-dir", str(tmp_path)], now=1000.0 + 3600)
    assert code == 1
    alerts = _alerts(tmp_path, "monitor-watch")
    assert set(alerts) == {"HEARTBEAT_STALE:opensearch-guard"}
    assert alerts["HEARTBEAT_STALE:opensearch-guard"]["level"] == "crit"


def test_a_missing_heartbeat_is_critical_not_ok(watch: ModuleType, tmp_path: Path) -> None:
    assert watch.main(["--state-dir", str(tmp_path)], now=5.0) == 1
    assert set(_alerts(tmp_path, "monitor-watch")) == {
        "HEARTBEAT_MISSING:opensearch-guard",
        "HEARTBEAT_MISSING:host-process-monitor",
    }


def test_the_alert_resolves_when_the_heartbeat_returns(watch: ModuleType, tmp_path: Path) -> None:
    args = ["--state-dir", str(tmp_path), "--component", "opensearch-guard=60"]
    _hb(tmp_path, "host-process-monitor", 100.0)
    _hb(tmp_path, "opensearch-guard", 0.0)
    assert watch.main(args, now=120.0) == 1
    _hb(tmp_path, "opensearch-guard", 119.0)
    assert watch.main(args, now=125.0) == 0
    assert _alerts(tmp_path, "monitor-watch") == {}


def test_show_alerts_lists_every_source_and_reflects_severity(
    watch: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "alerts-current.json").write_text(
        json.dumps(
            {
                "opensearch-guard": {"OPENSEARCH_STOPPED": {"level": "crit", "message": "stopped"}},
                "host-process-monitor": {"HOST_ZOMBIES": {"level": "warn", "message": "z"}},
            }
        )
    )
    assert watch.main(["--show-alerts", "--state-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "OPENSEARCH_STOPPED" in out and "HOST_ZOMBIES" in out
    (tmp_path / "alerts-current.json").write_text(
        json.dumps({"x": {"K": {"level": "warn", "message": "m"}}})
    )
    assert watch.main(["--show-alerts", "--state-dir", str(tmp_path)]) == 3
    (tmp_path / "alerts-current.json").write_text("{}")
    assert watch.main(["--show-alerts", "--state-dir", str(tmp_path)]) == 0


def _fake_proc(root: Path, zombies: int) -> None:
    root.mkdir(parents=True)
    for pid, (comm, state, ppid) in {
        1: ("systemd", "S", 0),
        100: ("temporal-server", "S", 1),
    }.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 0 0 0\n")
    for i in range(zombies):
        (root / str(1000 + i)).mkdir()
        (root / str(1000 + i) / "stat").write_text(f"{1000 + i} (grep) Z 100 0 0 0\n")


def _run_mon(
    mon: ModuleType, tmp: Path, zombies: int, now: float, extra: list[str] | None = None
) -> int:
    proc = tmp / f"proc{zombies}-{int(now)}"
    _fake_proc(proc, zombies)
    cg = tmp / "cg"
    cg.mkdir(exist_ok=True)
    return mon.main(
        ["--proc", str(proc), "--cgroup-root", str(cg), "--state-file", str(tmp / "state.json"),
         "--crit-parent-zombies", "5", *(extra or [])],
        now=now,
    )  # fmt: skip


def test_host_process_monitor_writes_heartbeat_and_pid_alerts_to_the_shared_path(
    mon: ModuleType, tmp_path: Path
) -> None:
    _run_mon(mon, tmp_path, 0, 100.0)
    assert (
        json.loads((tmp_path / "heartbeat-host-process-monitor.json").read_text())["level"] == "ok"
    )
    assert _alerts(tmp_path, "host-process-monitor") == {}
    _run_mon(mon, tmp_path, 8, 160.0)  # 1 つの親に 8 個 >= crit 5
    hb = json.loads((tmp_path / "heartbeat-host-process-monitor.json").read_text())
    assert hb["level"] == "crit" and hb["at"] == 160.0
    assert _alerts(tmp_path, "host-process-monitor")["HOST_ZOMBIES"]["level"] == "crit"
    _run_mon(mon, tmp_path, 0, 220.0)
    assert _alerts(tmp_path, "host-process-monitor") == {}  # 回復で resolved


def test_no_alerts_flag_writes_nothing_for_diagnostics(mon: ModuleType, tmp_path: Path) -> None:
    _run_mon(mon, tmp_path, 8, 1.0, ["--no-alerts"])
    assert not (tmp_path / "heartbeat-host-process-monitor.json").exists()
    assert not (tmp_path / "alerts-current.json").exists()


def test_guard_and_host_monitor_share_one_heartbeat_and_alert_directory(
    guard: ModuleType, mon: ModuleType, watch: ModuleType, tmp_path: Path
) -> None:
    """3 つが同じ dir で噛み合う: 両方が生きていれば無警報、guard だけが止まれば guard だけ警報。"""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 8000000 kB\n")
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable} -SE\nimport sys\nsys.exit(0)\n")
    docker.chmod(0o755)

    def run_guard(now: float) -> int:
        return guard.main(
            ["--docker", str(docker), "--meminfo", str(meminfo), "--state-dir", str(tmp_path),
             "--buffer-dir", str(tmp_path / "nobuf")],
            now=now,
        )  # fmt: skip

    run_guard(1000.0)
    _run_mon(mon, tmp_path, 0, 1000.0)
    assert watch.main(["--state-dir", str(tmp_path)], now=1060.0) == 0
    _run_mon(mon, tmp_path, 0, 4000.0)  # host monitor は動き続け、guard だけ止まる
    assert watch.main(["--state-dir", str(tmp_path)], now=4060.0) == 1
    assert set(_alerts(tmp_path, "monitor-watch")) == {"HEARTBEAT_STALE:opensearch-guard"}


def test_watcher_never_touches_docker_network_or_other_processes() -> None:
    src = (SCRIPTS / "monitor-watch.py").read_text().split('"""', 2)[2]
    for banned in ("subprocess", "socket", "urllib", "os.kill", "os.system", "docker"):
        assert banned not in src, banned
