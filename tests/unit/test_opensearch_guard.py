# ruff: noqa: E501  (全角文字を 2 幅で数えるため、日本語の説明文が 100 を超える)
"""scripts/opensearch-guard.py: メモリ保護の hysteresis・安全な自動復旧・OpenSearch 非依存の警報。

理由は docs/testing/opensearch-guard-rationale.md。偽の /proc/meminfo・偽の docker CLI・偽の時計だけを使い、
実ホストの docker・systemd・ネットワークには触れない（INV-79 / INV-80）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "opensearch-guard.py"
ALERTS = ROOT / "scripts" / "avp_alerts.py"

FAKE_DOCKER = """#!{python} -SE
import json, os, sys
path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
with open(path + ".calls", "a") as f:
    f.write(json.dumps(argv) + "\\n")
def save():
    json.dump(state, open(path, "w"))
def labels():
    out = {{}}
    for i, a in enumerate(argv):
        if a == "--filter" and argv[i + 1].startswith("label="):
            k, _, v = argv[i + 1][len("label="):].partition("=")
            out[k] = v
    return out
def match(c, want):
    return all({{"com.docker.compose.project": c["project"],
                "com.docker.compose.service": c["service"]}}.get(k) == v for k, v in want.items())
cmd = argv[0]
if cmd == "ps":
    want = labels()
    rows = [c for c in state["containers"] if match(c, want) and ("-a" in argv or c["status"] == "running")]
    if "-q" in argv:
        print("\\n".join(c["id"] for c in rows))
    else:
        for c in rows:
            print(c["name"] + "\\t" + c["ps"])
elif cmd == "inspect":
    c = next(c for c in state["containers"] if c["id"] == argv[-1])
    st = {{"Status": c["status"], "ExitCode": c["exit_code"], "FinishedAt": c["finished_at"]}}
    if c.get("health"):
        st["Health"] = {{"Status": c["health"]}}
    print(json.dumps(st))
elif cmd == "stop":
    for cid in argv[1:]:
        c = next(c for c in state["containers"] if c["id"] == cid)
        c["status"] = "exited"; c["ps"] = "Exited (143) 1 second ago"; c["exit_code"] = 143
    save()
elif cmd == "start":
    for cid in argv[1:]:
        c = next(c for c in state["containers"] if c["id"] == cid)
        c["status"] = "running"; c["ps"] = "Up 2 seconds (health: starting)"; c["health"] = "starting"
    save()
elif cmd == "logs":
    c = next(c for c in state["containers"] if c["id"] == argv[-1])
    if c["status"] != "running":
        print("Error: container is not running", file=sys.stderr); sys.exit(1)
    sys.stdout.write(state.get("logs", {{}}).get(c["id"], ""))
else:
    print("fake docker: unsupported " + cmd, file=sys.stderr); sys.exit(2)
"""


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    return _load(SCRIPT, "avp_opensearch_guard")


def _c(cid: str, name: str, project: str, service: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": cid * 8,
        "name": name,
        "project": project,
        "service": service,
        "status": "running",
        "ps": "Up 3 days (healthy)",
        "health": "healthy",
        "exit_code": 0,
        "finished_at": "0001-01-01T00:00:00Z",
    }
    base.update(kw)
    return base


class Host:
    """偽のホスト。時計は 30 秒ずつ進め、tick のたびに guard を 1 回動かす。"""

    def __init__(self, tmp: Path, guard: ModuleType) -> None:
        self.g = guard
        self.tmp = tmp
        self.now = 1_800_000_000.0
        self.meminfo = tmp / "meminfo"
        self.state = tmp / "docker.json"
        self.out = tmp / "state"
        self.buffer = tmp / "buffer"
        self.buffer.mkdir()
        self.docker = tmp / "docker"
        self.docker.write_text(FAKE_DOCKER.format(python=sys.executable))
        self.docker.chmod(self.docker.stat().st_mode | stat.S_IXUSR)
        self.containers = [
            _c("a", "avp2-logging-opensearch-1", "avp2-logging", "opensearch"),
            _c("b", "avp2-logging-dashboards-1", "avp2-logging", "dashboards"),
            _c("c", "avp2-logging-fluent-bit-1", "avp2-logging", "fluent-bit"),
            _c("d", "avp2-temporal-1", "avp2", "temporal"),
            _c("e", "avp2-postgres-1", "avp2", "postgres"),
            _c("f", "avp2-pipeline-worker-1", "avp2", "pipeline-worker"),
        ]
        self.logs: dict[str, str] = {}
        self.save()
        self.extra: list[str] = []
        self.stderr: list[str] = []

    def save(self) -> None:
        self.state.write_text(json.dumps({"containers": self.containers, "logs": self.logs}))

    def load(self) -> None:
        self.containers = json.loads(self.state.read_text())["containers"]

    def c(self, service: str) -> dict[str, object]:
        self.load()
        return next(c for c in self.containers if c["service"] == service)

    def set(self, service: str, **kw: object) -> None:
        self.load()
        next(c for c in self.containers if c["service"] == service).update(kw)
        self.save()

    def tick(self, mib: int | None, *, dt: float = 30.0, flags: tuple[str, ...] = ()) -> int:
        self.now += dt
        if mib is not None:
            self.meminfo.write_text(f"MemTotal: 15500000 kB\nMemAvailable: {mib * 1024} kB\n")
        elif self.meminfo.exists():
            self.meminfo.unlink()
        import os

        os.environ["FAKE_DOCKER_STATE"] = str(self.state)
        argv = [
            "--docker", str(self.docker),
            "--meminfo", str(self.meminfo),
            "--state-dir", str(self.out),
            "--buffer-dir", str(self.buffer),
            *flags, *self.extra,
        ]  # fmt: skip
        return self.g.main(argv, now=self.now)

    def calls(self) -> list[list[str]]:
        p = Path(str(self.state) + ".calls")
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def mutations(self) -> list[list[str]]:
        return [
            c
            for c in self.calls()
            if c[0] in ("stop", "start", "rm", "kill", "restart", "run", "compose")
        ]

    def guard_state(self) -> dict[str, object]:
        return json.loads((self.out / "guard-state.json").read_text())

    def alerts(self) -> dict[str, dict[str, object]]:
        p = self.out / "alerts-current.json"
        data = json.loads(p.read_text()) if p.exists() else {}
        return data.get("opensearch-guard", {})


@pytest.fixture
def host(tmp_path: Path, guard: ModuleType) -> Host:
    return Host(tmp_path, guard)


def _sustained_low(h: Host, mib: int = 1000, n: int = 8) -> None:
    for _ in range(n):
        h.tick(mib)


# ---------------------------------------------------------------- test 11: 一時的な低下
def test_transient_dip_below_threshold_stops_nothing(host: Host) -> None:
    """2026-10-21 の再発防止: 1 サンプル（1039MiB）だけの低下で止めない。"""
    for mib in (5000, 5000, 1039, 5400, 5400, 5400):
        host.tick(mib)
    assert host.mutations() == []
    assert host.c("opensearch")["status"] == "running"


def test_dip_is_visible_as_a_warning_then_resolves(host: Host) -> None:
    host.tick(5000)
    host.tick(1039)
    assert "MEMORY_LOW" in host.alerts()
    host.tick(5000)
    assert "MEMORY_LOW" not in host.alerts()


def test_oscillating_memory_never_starts_a_stop_start_loop(host: Host) -> None:
    for i in range(60):
        host.tick(1000 if i % 2 == 0 else 5000)
    assert host.mutations() == []


def test_a_long_gap_between_samples_breaks_the_low_run(host: Host) -> None:
    """timer が止まっていた間の古い低サンプルを「継続」に数えない。"""
    for _ in range(4):
        host.tick(1000)
    host.tick(1000, dt=3600)  # 1 時間空いた
    assert host.mutations() == []


def test_sustained_pressure_still_stops_opensearch_and_dashboards_only(host: Host) -> None:
    """保護の目的は残る。止めるのは avp2-logging の opensearch と dashboards だけ。"""
    _sustained_low(host)
    assert host.c("opensearch")["status"] == "exited"
    assert host.c("dashboards")["status"] == "exited"
    for svc in ("fluent-bit", "temporal", "postgres", "pipeline-worker"):
        assert host.c(svc)["status"] == "running", svc
    stopped = {c[1] for c in host.mutations() if c[0] == "stop"}
    assert stopped <= {"a" * 8, "b" * 8}


def test_the_stop_needs_n_samples_and_minimum_duration(host: Host) -> None:
    # 4 サンプルでは止めない（既定 N=5）
    for _ in range(4):
        host.tick(1000)
    assert host.mutations() == []
    # 5 サンプル目で N は満たすが、継続時間 180 秒（30 秒 x 4 = 120 秒）に足りない
    host.tick(1000)
    assert host.mutations() == []
    # 継続時間を満たすと止める
    host.tick(1000)
    host.tick(1000)
    assert host.c("opensearch")["status"] == "exited"


# ---------------------------------------------------------------- test 10: OpenSearch が止まる
def test_opensearch_stop_raises_alert_records_state_and_spares_others(host: Host) -> None:
    _sustained_low(host)
    alerts = host.alerts()
    assert alerts["OPENSEARCH_STOPPED"]["level"] == "crit"
    st = host.guard_state()
    rec = st["stopped"]
    assert isinstance(rec, dict)
    assert "MemAvailable" in rec["reason"]
    assert rec["stopped_at"] > 0
    assert len(rec["samples"]) >= 5 and all("mib" in s and "t" in s for s in rec["samples"])
    cond = rec["resume_conditions"]
    assert cond["mem_available_mib"] == 4096 and cond["consecutive_samples"] == 3
    assert cond["workers_healthy"] is True and cond["scope"] == "opensearch container only"
    assert st["restart_attempts"] == []
    # journal 向けの行と履歴ファイル（OpenSearch に依存しない）
    assert (host.out / "alerts.jsonl").read_text().count("OPENSEARCH_STOPPED") >= 1


def test_alert_is_written_to_stderr_with_a_journal_priority(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    _sustained_low(host)
    err = capsys.readouterr().err
    assert re.search(
        r"^<2>avp-alert alert source=opensearch-guard key=OPENSEARCH_STOPPED ", err, re.M
    )


def test_opensearch_stopped_longer_than_threshold_alerts(host: Host) -> None:
    host.extra = ["--stopped-alert-s", "1800"]
    _sustained_low(host)
    host.tick(1000, dt=60)
    assert "OPENSEARCH_STOPPED_LONG" not in host.alerts()
    host.tick(1000, dt=1900)
    assert host.alerts()["OPENSEARCH_STOPPED_LONG"]["level"] == "crit"


def test_dry_run_changes_nothing(host: Host) -> None:
    for _ in range(8):
        host.tick(1000, flags=("--dry-run",))
    assert host.mutations() == []
    assert "OPENSEARCH_STOPPED" not in host.alerts()
    assert "MEMORY_SUSTAINED_LOW" in host.alerts()


def test_unreadable_meminfo_neither_stops_nor_claims_ok(host: Host) -> None:
    host.tick(None)
    assert host.mutations() == []
    assert host.alerts()["MEMINFO_UNREADABLE"]["level"] == "warn"


# ---------------------------------------------------------------- 自動復旧
def _stopped_by_guard(h: Host, flags: tuple[str, ...] = ("--auto-restart",)) -> None:
    h.extra += ["--max-sample-gap-s", "100000"]  # 時間を飛ばす試験のため。既定の挙動は別の試験
    for _ in range(8):
        h.tick(1000, flags=flags)
    assert h.c("opensearch")["status"] == "exited"


def test_auto_restart_is_off_unless_the_flag_is_given(host: Host) -> None:
    _stopped_by_guard(host, flags=())
    for _ in range(40):
        host.tick(9000)
    assert host.c("opensearch")["status"] == "exited"
    assert not [c for c in host.mutations() if c[0] == "start"]


def test_auto_restart_starts_only_the_opensearch_container(host: Host) -> None:
    _stopped_by_guard(host)
    for _ in range(40):
        host.tick(9000, flags=("--auto-restart",))
    starts = [c for c in host.mutations() if c[0] == "start"]
    assert starts == [["start", "a" * 8]]
    assert host.c("opensearch")["status"] == "running"
    # compose / recreate / rm / run は一切呼ばない
    assert not [c for c in host.calls() if c[0] in ("compose", "rm", "run", "restart", "create")]
    assert len(host.guard_state()["restart_attempts"]) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize("mib", [4095, 3000])
def test_restart_waits_for_the_restart_line(host: Host, mib: int) -> None:
    _stopped_by_guard(host)
    for _ in range(40):
        host.tick(mib, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"


def test_restart_needs_three_consecutive_samples_above_the_line(host: Host) -> None:
    _stopped_by_guard(host)
    host.tick(9000, dt=700, flags=("--auto-restart",))
    host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"  # まだ 2 サンプル
    host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "running"


def test_a_dip_inside_the_restart_run_resets_the_count(host: Host) -> None:
    _stopped_by_guard(host)
    for mib in (9000, 9000, 3000, 9000, 9000):
        host.tick(mib, dt=400, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"


@pytest.mark.parametrize(
    "bad",
    [
        {"ps": "Up 3 days (unhealthy)", "health": "unhealthy"},
        {"ps": "Up 2 seconds (health: starting)", "health": "starting"},
        {"ps": "Restarting (1) 5 seconds ago", "status": "restarting"},
        {"ps": "Exited (1) 2 minutes ago", "status": "exited"},
    ],
)
def test_restart_requires_all_workers_healthy(host: Host, bad: dict[str, object]) -> None:
    _stopped_by_guard(host)
    host.set("pipeline-worker", **bad)
    for _ in range(40):
        host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"
    assert "RESTART_BLOCKED" in host.alerts()


def test_one_shot_exited_zero_container_does_not_block_restart(host: Host) -> None:
    _stopped_by_guard(host)
    host.set("postgres", ps="Exited (0) 3 days ago", status="exited")
    for _ in range(40):
        host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "running"


def test_never_restarts_an_opensearch_the_guard_did_not_stop(host: Host) -> None:
    """止めたのが別の人・古い timer なら、guard は勝手に起こさない。警報だけ。"""
    host.set("opensearch", status="exited", ps="Exited (143) 4 days ago",
             finished_at="2026-10-20T15:02:01Z")  # fmt: skip
    for _ in range(40):
        host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"
    assert host.alerts()["OPENSEARCH_DOWN_UNMANAGED"]["level"] == "crit"
    assert host.mutations() == []


def test_min_stopped_time_prevents_an_immediate_bounce(host: Host) -> None:
    _stopped_by_guard(host)
    for _ in range(6):
        host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"  # 3 分では起こさない（既定 10 分）


# ---------------------------------------------------------------- flap guard
def _cycle(h: Host) -> None:
    """低下 -> stop -> 回復 -> start までを 1 周。時間は十分に空ける。"""
    flags = ("--auto-restart",)
    if "--max-sample-gap-s" not in h.extra:
        h.extra += ["--max-sample-gap-s", "100000"]
    for _ in range(8):
        h.tick(1000, flags=flags)
    for _ in range(3):
        h.tick(9000, dt=4000, flags=flags)  # 復旧条件。backoff も越える


def test_flap_cap_stays_stopped_and_alerts_after_max_restarts(host: Host) -> None:
    host.extra = ["--max-restarts-24h", "3"]
    for _ in range(3):
        _cycle(host)
    assert host.c("opensearch")["status"] == "running"
    assert len([c for c in host.mutations() if c[0] == "start"]) == 3
    # 4 周目: 止まる。メモリが回復しても起こさない
    for _ in range(8):
        host.tick(1000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"
    for _ in range(10):
        host.tick(9000, dt=4000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"
    assert len([c for c in host.mutations() if c[0] == "start"]) == 3
    assert host.alerts()["RESTART_CAPPED"]["level"] == "crit"


def test_flap_cap_is_released_after_24_hours(host: Host) -> None:
    host.extra = ["--max-restarts-24h", "1"]
    _cycle(host)
    for _ in range(8):
        host.tick(1000, flags=("--auto-restart",))
    for _ in range(4):
        host.tick(9000, dt=4000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "exited"
    host.tick(9000, dt=90000, flags=("--auto-restart",))
    for _ in range(3):
        host.tick(9000, flags=("--auto-restart",))
    assert host.c("opensearch")["status"] == "running"


def test_backoff_doubles_between_attempts(host: Host) -> None:
    host.extra = ["--max-restarts-24h", "5", "--restart-backoff-s", "600"]
    flags = ("--auto-restart",)
    _cycle(host)  # 1 回目の restart
    for _ in range(8):
        host.tick(1000, flags=flags)
    # 2 回目は直近の attempt から 600 x 2 = 1200 秒後まで起こさない（停止から 10 分は過ぎている）
    for _ in range(3):
        host.tick(9000, dt=250, flags=flags)
    assert host.c("opensearch")["status"] == "exited"
    for _ in range(4):
        host.tick(9000, dt=250, flags=flags)
    assert host.c("opensearch")["status"] == "running"


# ---------------------------------------------------------------- Fluent Bit の警報
def _chunk(h: Host, name: str, size: int, age_s: float) -> None:
    f = h.buffer / name
    f.write_bytes(b"x" * size)
    import os

    os.utime(f, (h.now - age_s, h.now - age_s))


def test_buffer_near_its_limit_alerts_before_any_drop(host: Host) -> None:
    host.extra = ["--buffer-limit-bytes", "1000"]
    _chunk(host, "c1", 500, 10)
    host.tick(5000)
    assert "FLUENT_BIT_BUFFER_NEAR_LIMIT" not in host.alerts()
    _chunk(host, "c2", 300, 10)  # 80%
    host.tick(5000)
    assert host.alerts()["FLUENT_BIT_BUFFER_NEAR_LIMIT"]["level"] == "warn"
    _chunk(host, "c3", 150, 10)  # 95%
    host.tick(5000)
    assert host.alerts()["FLUENT_BIT_BUFFER_NEAR_LIMIT"]["level"] == "crit"


def test_oldest_chunk_nearing_the_retry_window_alerts_before_drops(host: Host) -> None:
    host.extra = ["--retry-window-s", "10000"]
    _chunk(host, "c1", 10, 4000)
    host.tick(5000)
    assert "FLUENT_BIT_CHUNK_AGE" not in host.alerts()
    _chunk(host, "c2", 10, 6000)  # 60% -> warn
    host.tick(5000)
    assert host.alerts()["FLUENT_BIT_CHUNK_AGE"]["level"] == "warn"
    _chunk(host, "c3", 10, 9000)  # 90% -> crit
    host.tick(5000)
    assert host.alerts()["FLUENT_BIT_CHUNK_AGE"]["level"] == "crit"


def test_dropped_chunks_in_fluent_bit_log_raise_a_crit_alert(host: Host) -> None:
    host.logs["c" * 8] = (
        "[2026/10/25 00:30:00] [ warn] [engine] chunk '1-1.flb' cannot be retried: task_id=1, input=emitter.2 > output=avp_app\n"
        "[2026/10/25 00:31:00] [ warn] [engine] chunk '2-2.flb' cannot be retried: task_id=2, input=emitter.2 > output=avp_app\n"
        "[2026/10/25 00:31:30] [ info] unrelated\n"
    )
    host.save()
    host.tick(5000)
    a = host.alerts()["FLUENT_BIT_DROPPED"]
    assert a["level"] == "crit"
    assert a["detail"]["dropped_chunks_24h"] == 2  # type: ignore[index]


def test_fluent_bit_not_running_is_alerted(host: Host) -> None:
    host.set("fluent-bit", status="exited")
    host.tick(5000)
    assert host.alerts()["FLUENT_BIT_NOT_RUNNING"]["level"] == "crit"


# ---------------------------------------------------------------- 契約・安全
def test_heartbeat_is_written_every_run_even_when_everything_is_fine(host: Host) -> None:
    host.tick(5000)
    hb = json.loads((host.out / "heartbeat-opensearch-guard.json").read_text())
    assert hb["at"] == host.now and hb["level"] == "ok"
    host.tick(None)  # 読めなくても heartbeat は出る（生きてはいる）
    assert json.loads((host.out / "heartbeat-opensearch-guard.json").read_text())["level"] != "ok"


def test_refuses_to_target_the_application_project(host: Host) -> None:
    host.extra = ["--project", "avp2"]
    with pytest.raises(SystemExit) as e:
        host.tick(5000)
    assert e.value.code == 2


def test_guard_source_never_uses_compose_or_destructive_docker_verbs() -> None:
    src = SCRIPT.read_text().split('"""', 2)[2]  # docstring は説明なので除く
    for banned in ('"compose"', "compose up", "docker compose", '"rm"', '"down"', '"prune"',
                   '"kill"', '"restart"', '"create"', "--force-recreate", "shell=True", "os.kill"):  # fmt: skip
        assert banned not in src, banned
    verbs = set(re.findall(r'\.run\(\s*"(\w+)"', src))
    assert verbs <= {"ps", "inspect", "stop", "start", "logs"}, verbs


def test_guard_constants_match_the_fluent_bit_config_and_documented_conditions(
    guard: ModuleType,
) -> None:
    """定義は 1 箇所（AGENTS §8）。Fluent Bit の設定と食い違ったら落ちる。"""
    yaml = (ROOT / "deploy/logging/fluent-bit/fluent-bit.yaml").read_text()
    limits = {int(x) for x in re.findall(r"^\s*retry_limit:\s*(\d+)", yaml, re.M)}
    assert limits == {guard.FB_RETRY_LIMIT}
    cap = int(re.search(r"^\s*scheduler\.cap:\s*(\d+)", yaml, re.M).group(1))  # type: ignore[union-attr]
    assert cap == guard.FB_SCHEDULER_CAP_S
    compose = (ROOT / "deploy/logging/compose.logging.yaml").read_text()
    assert "AVP_LOG_STORAGE_LIMIT:-1G" in compose and guard.FB_STORAGE_LIMIT_BYTES == 1 << 30
    assert guard.RESTART_MEM_MIB == 4096 and guard.RESTART_SAMPLES == 3
    assert guard.STOP_MEM_MIB == 1536 and guard.DASHBOARDS_STOP_MEM_MIB == 2048
