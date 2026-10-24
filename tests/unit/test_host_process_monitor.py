"""scripts/host-process-monitor.py: 読み取り専用の PID / zombie 監視。

理由は docs/testing/host-process-monitor-rationale.md。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "host-process-monitor.py"


@pytest.fixture(scope="module")
def mon() -> ModuleType:
    spec = importlib.util.spec_from_file_location("avp_host_process_monitor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _proc(root: Path, pid: int, comm: str, state: str, ppid: int) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    # /proc/<pid>/stat: pid (comm) state ppid ...。comm は空白・括弧を含みうる
    (d / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 0 0 0\n")


def _fake_host(tmp_path: Path, zombies_of: dict[int, int], extra: int = 0) -> Path:
    root = tmp_path / "proc"
    root.mkdir(parents=True)
    _proc(root, 1, "systemd", "S", 0)
    _proc(root, 100, "temporal-server", "S", 1)
    pid = 1000
    for parent, n in zombies_of.items():
        for _ in range(n):
            _proc(root, pid, "grep", "Z", parent)
            pid += 1
    for _ in range(extra):
        _proc(root, pid, "sleep", "S", 1)
        pid += 1
    (root / "loadavg").write_text(f"0.1 0.1 0.1 1/{pid - 1000 + 2} 9999\n")
    return root


def test_no_zombies_is_ok(mon: ModuleType, tmp_path: Path) -> None:
    root = _fake_host(tmp_path, {})
    r = mon.scan_zombies(root)
    assert r.total == 0 and r.by_parent == {}
    assert mon.judge_zombies(r, warn_total=20, warn_parent=10, crit_parent=200) == "ok"


def test_zombies_are_counted_by_parent_and_judged(mon: ModuleType, tmp_path: Path) -> None:
    root = _fake_host(tmp_path, {100: 12, 1: 2})
    r = mon.scan_zombies(root)
    assert r.total == 14
    assert r.by_parent == {100: 12, 1: 2}
    assert r.parent_comm[100] == "temporal-server"
    assert mon.judge_zombies(r, warn_total=20, warn_parent=10, crit_parent=200) == "warn"
    big = mon.scan_zombies(_fake_host(tmp_path / "b", {100: 250}))
    assert mon.judge_zombies(big, warn_total=20, warn_parent=10, crit_parent=200) == "crit"


def test_comm_with_space_and_paren_is_parsed(mon: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "proc"
    root.mkdir()
    _proc(root, 50, "Web Content (x) y", "Z", 7)
    _proc(root, 7, "a b", "S", 1)
    r = mon.scan_zombies(root)
    assert r.by_parent == {7: 1}
    assert r.parent_comm[7] == "a b"


def test_process_vanishing_or_unreadable_is_skipped_not_fatal(
    mon: ModuleType, tmp_path: Path
) -> None:
    root = _fake_host(tmp_path, {100: 1})
    (root / "2000").mkdir()  # stat が無い（読む前に消えた）
    (root / "2001").mkdir()
    (root / "2001" / "stat").write_text("garbage")
    (root / "self").mkdir()  # 数字以外は無視
    assert mon.scan_zombies(root).total == 1


def test_cgroup_pids_ratio(mon: ModuleType, tmp_path: Path) -> None:
    cg = tmp_path / "cg"
    a = cg / "docker-aaa.scope"
    a.mkdir(parents=True)
    (a / "pids.max").write_text("1000\n")
    (a / "pids.current").write_text("950\n")
    b = cg / "docker-bbb.scope"
    b.mkdir()
    (b / "pids.max").write_text("max\n")  # 無制限は比率を出さない
    (b / "pids.current").write_text("5000\n")
    c = cg / "docker-ccc.scope"
    c.mkdir()  # pids controller が無い
    rows = mon.scan_cgroup_pids(cg)
    assert [(r.name, r.current, r.limit) for r in rows] == [
        ("docker-aaa.scope", 950, 1000),
        ("docker-bbb.scope", 5000, None),
    ]
    assert mon.judge_cgroup(rows, warn_ratio=0.7, crit_ratio=0.9) == "crit"
    assert mon.judge_cgroup(rows[1:], warn_ratio=0.7, crit_ratio=0.9) == "ok"


def test_cgroup_root_missing_is_unknown_not_ok(mon: ModuleType, tmp_path: Path) -> None:
    assert mon.scan_cgroup_pids(tmp_path / "nope") is None
    assert mon.judge_cgroup(None, warn_ratio=0.7, crit_ratio=0.9) == "unknown"


def test_the_monitor_never_signals_or_writes_outside_its_state_dir(mon: ModuleType) -> None:
    src = SCRIPT.read_text()
    for banned in (
        "os.kill",
        "signal.",
        "subprocess",
        "docker stop",
        "docker kill",
        "killpg",
        "os.system",
    ):
        assert banned not in src, banned


def test_output_is_quiet_when_ok_and_unchanged_and_loud_on_change(
    mon: ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    # 初回 ok: heartbeat 行を1度だけ
    assert mon.decide_output(state, "ok", now=1000, heartbeat_s=3600) == "heartbeat"
    # 同じ ok を heartbeat 間隔内で繰り返しても黙る
    assert mon.decide_output(state, "ok", now=1060, heartbeat_s=3600) is None
    # ok のまま heartbeat 間隔を超えたら1行
    assert mon.decide_output(state, "ok", now=1000 + 3601, heartbeat_s=3600) == "heartbeat"
    # 状態が変われば出す
    assert mon.decide_output(state, "warn", now=5000, heartbeat_s=3600) == "change"
    # warn が続く間は、再通知間隔（heartbeat）までは黙る（毎分同じ行を出さない）
    assert mon.decide_output(state, "warn", now=5060, heartbeat_s=3600) is None
    assert mon.decide_output(state, "ok", now=5120, heartbeat_s=3600) == "change"
    json.loads(state.read_text())  # 壊れていない


def test_corrupt_state_file_is_treated_as_first_run(mon: ModuleType, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json")
    assert mon.decide_output(state, "ok", now=1, heartbeat_s=3600) == "heartbeat"


def test_exit_code_reflects_worst_level(mon: ModuleType) -> None:
    assert mon.exit_code(["ok", "ok"]) == 0
    assert mon.exit_code(["ok", "warn"]) == 1
    assert mon.exit_code(["warn", "crit", "unknown"]) == 2
    # 読めない（unknown）は ok と偽らない
    assert mon.exit_code(["ok", "unknown"]) == 3


def test_main_runs_against_a_fake_host_and_reports_json(
    mon: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_host(tmp_path, {100: 30})
    code = mon.main(
        [
            "--proc",
            str(root),
            "--cgroup-root",
            str(tmp_path / "nocg"),
            "--state-file",
            str(tmp_path / "s.json"),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["zombies"]["total"] == 30
    assert out["zombies"]["top_parent"] == {"pid": 100, "comm": "temporal-server", "count": 30}
    assert out["level"] == "warn"
    assert out["incomplete"] is True  # cgroup が読めなかったことを ok と偽らない
    assert code == 3
