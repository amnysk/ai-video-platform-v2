#!/usr/bin/env python3
"""ホストの PID / zombie 監視（読み取り専用）。

2026-10-21 の avp2-temporal-1（zombie 17,934 個・cgroup pids.max 到達。ADR-0052）の再発を、
**手遅れになる前に**検知するための観測。何も止めない・kill しない・docker を呼ばない。
通常ユーザーで動く（root 不要。rootless Docker の cgroup は user@UID.service 配下で読める）。

    scripts/host-process-monitor.py [--proc /proc] [--cgroup-root DIR] [--state-file FILE]
        [--heartbeat-s 21600] [--json]

見るもの:
  zombie   /proc/<pid>/stat の state=Z を親 pid ごとに数える（親が 1 つで膨らむのが leak の形）
  cgroup   <cgroup-root> 直下の docker-*.scope の pids.current / pids.max（上限の何割か）
終了コード: 0 ok / 1 warn / 2 crit / 3 読めないものがある（ok と偽らない。crit があれば 2 が優先）
出力: ok のままなら heartbeat 間隔でしか出さない。状態が変わったときだけ即出す
      （1 分毎の timer でも journal を埋めない）。詳細（上位の親）は warn 以上のときだけ。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from avp_alerts import Alert, AlertSink, write_heartbeat  # noqa: E402

LEVELS = ("ok", "warn", "crit")


@dataclass
class ZombieReport:
    total: int = 0
    by_parent: dict[int, int] = field(default_factory=dict)
    parent_comm: dict[int, str] = field(default_factory=dict)


@dataclass
class CgroupRow:
    name: str
    current: int
    limit: int | None  # None = 無制限（max）


def _read_stat(path: Path) -> tuple[str, str, int] | None:
    """(comm, state, ppid)。読めない・壊れている（読む前に消えた等）は None。"""
    try:
        text = path.read_text()
    except OSError:
        return None
    # comm は空白・括弧を含みうるので、最後の ')' で切る
    left, right = text.find("("), text.rfind(")")
    if left < 0 or right < left:
        return None
    right_fields = text[right + 1 :].split()
    if len(right_fields) < 2:
        return None
    try:
        return text[left + 1 : right], right_fields[0], int(right_fields[1])
    except ValueError:
        return None


def scan_zombies(proc: Path) -> ZombieReport:
    by_parent: Counter[int] = Counter()
    stats: dict[int, tuple[str, str, int]] = {}
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return ZombieReport()
    for e in entries:
        s = _read_stat(proc / e / "stat")
        if s is None:
            continue
        stats[int(e)] = s
        if s[1] == "Z":
            by_parent[s[2]] += 1
    report = ZombieReport(total=sum(by_parent.values()), by_parent=dict(by_parent))
    for parent in by_parent:
        if parent in stats:
            report.parent_comm[parent] = stats[parent][0]
    return report


def judge_zombies(r: ZombieReport, *, warn_total: int, warn_parent: int, crit_parent: int) -> str:
    top = max(r.by_parent.values(), default=0)
    if top >= crit_parent:
        return "crit"
    if top >= warn_parent or r.total >= warn_total:
        return "warn"
    return "ok"


def scan_cgroup_pids(root: Path) -> list[CgroupRow] | None:
    """docker-*.scope の pids。root が無ければ None（監視できていない = unknown）。"""
    if not root.is_dir():
        return None
    rows: list[CgroupRow] = []
    for d in sorted(root.glob("docker-*.scope")):
        try:
            current = int((d / "pids.current").read_text().strip())
            raw = (d / "pids.max").read_text().strip()
            limit = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            continue
        rows.append(CgroupRow(d.name, current, limit))
    return rows


def judge_cgroup(rows: list[CgroupRow] | None, *, warn_ratio: float, crit_ratio: float) -> str:
    if rows is None:
        return "unknown"
    worst = "ok"
    for r in rows:
        if not r.limit:
            continue
        ratio = r.current / r.limit
        if ratio >= crit_ratio:
            return "crit"
        if ratio >= warn_ratio:
            worst = "warn"
    return worst


def exit_code(levels: list[str]) -> int:
    if "crit" in levels:
        return 2
    if "unknown" in levels:
        return 3
    return 1 if "warn" in levels else 0


def decide_output(state_file: Path, level: str, *, now: float, heartbeat_s: int) -> str | None:
    """'change'（状態が変わった）/ 'heartbeat'（同じ状態を間隔ごとに）/ None（黙る）。"""
    try:
        state = json.loads(state_file.read_text())
        last_level, last_emit = state["level"], float(state["emitted_at"])
    except (OSError, ValueError, KeyError, TypeError):
        last_level, last_emit = None, None
    if last_level is None:
        decision: str | None = "heartbeat"
    elif last_level != level:
        decision = "change"
    elif now - float(last_emit or 0) >= heartbeat_s:
        decision = "heartbeat"
    else:
        decision = None
    if decision is not None:
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"level": level, "emitted_at": now}))
            tmp.replace(state_file)  # 原子的に置き換える（途中で壊れた state を残さない）
        except OSError:
            pass  # state が書けなくても監視は続ける（毎回出るだけ）
    return decision


def publish_alerts(directory: Path, report: dict[str, object], *, now: float) -> None:
    """heartbeat と警報を共通の口（avp_alerts。OpenSearch に依存しない）へ出す（ADR-0059 / INV-80）。"""  # noqa: E501
    level = str(report["level"])
    write_heartbeat(directory, "host-process-monitor", now=now, level=level)
    active: dict[str, Alert] = {}
    zom = report["zombies"]
    cg = report["cgroup_pids"]
    assert isinstance(zom, dict) and isinstance(cg, dict)
    for key, part, what in (
        ("HOST_ZOMBIES", zom, "zombie"),
        ("HOST_CGROUP_PIDS", cg, "cgroup pids"),
    ):
        if part["level"] in ("warn", "crit", "unknown"):
            active[key] = Alert(
                key,
                "crit" if part["level"] == "crit" else "warn",
                f"{what}: {part['level']}（{json.dumps(part, ensure_ascii=False, sort_keys=True)}）",  # noqa: E501
            )
    AlertSink(directory, now=now).sync("host-process-monitor", active)


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    uid = os.getuid()
    ap.add_argument("--proc", default="/proc")
    ap.add_argument(
        "--cgroup-root",
        default=f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/user.slice",
    )
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    ap.add_argument("--state-file", default=f"{state_home}/avp-host-monitor/state.json")
    ap.add_argument("--heartbeat-s", type=int, default=6 * 3600)
    ap.add_argument("--warn-zombies", type=int, default=20)
    ap.add_argument("--warn-parent-zombies", type=int, default=10)
    ap.add_argument("--crit-parent-zombies", type=int, default=200)
    ap.add_argument("--warn-pids-ratio", type=float, default=0.7)
    ap.add_argument("--crit-pids-ratio", type=float, default=0.9)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--alerts-dir",
        default=None,
        help="heartbeat と警報の置き場（既定: --state-file と同じ dir）",
    )
    ap.add_argument("--no-alerts", action="store_true", help="heartbeat・警報を書かない（診断用）")
    a = ap.parse_args(argv)
    clock = time.time() if now is None else now

    z = scan_zombies(Path(a.proc))
    zl = judge_zombies(
        z,
        warn_total=a.warn_zombies,
        warn_parent=a.warn_parent_zombies,
        crit_parent=a.crit_parent_zombies,
    )
    rows = scan_cgroup_pids(Path(a.cgroup_root))
    cl = judge_cgroup(rows, warn_ratio=a.warn_pids_ratio, crit_ratio=a.crit_pids_ratio)
    levels = [zl, cl]
    known = [lv for lv in levels if lv in LEVELS]
    level = max(known, key=LEVELS.index) if known else "unknown"
    top = max(z.by_parent.items(), key=lambda kv: kv[1], default=None)
    report = {
        "level": level,
        "incomplete": "unknown" in levels,
        "zombies": {
            "level": zl,
            "total": z.total,
            "top_parent": None
            if top is None
            else {"pid": top[0], "comm": z.parent_comm.get(top[0], "?"), "count": top[1]},
        },
        "cgroup_pids": {
            "level": cl,
            "top": None
            if not rows
            else max(
                ({"name": r.name, "current": r.current, "limit": r.limit} for r in rows),
                key=lambda d: d["current"] / d["limit"] if d["limit"] else 0,
            ),
        },
    }
    decision = decide_output(
        Path(a.state_file),
        "unknown" if report["incomplete"] and level == "ok" else level,
        now=clock,
        heartbeat_s=a.heartbeat_s,
    )
    if not a.no_alerts:
        publish_alerts(Path(a.alerts_dir or Path(a.state_file).parent), report, now=clock)
    if decision is not None or a.json:
        line = json.dumps(report, ensure_ascii=False, sort_keys=True)
        print(line if a.json else f"{decision} {line}")
    return exit_code(levels)


if __name__ == "__main__":
    sys.exit(main())
