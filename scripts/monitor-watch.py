#!/usr/bin/env python3
# ruff: noqa: E501  (全角文字を 2 幅で数えるため、日本語の説明文が 100 を超える)
"""監視の dead-man's switch（ADR-0059 / INV-80）。**読み取り専用**。

opensearch-guard と host-process-monitor は毎回 ``heartbeat-<component>.json`` を書く。この script は
**別の systemd timer** から動き、heartbeat が古い・無いものを警報にする。監視が黙って止まっても
（2026-10-21 からの 4 日間のように）気付けるようにするための、監視される側と独立した見張り。

    scripts/monitor-watch.py [--state-dir DIR] [--component NAME=MAX_AGE_S ...]
    scripts/monitor-watch.py --show-alerts [--state-dir DIR]   # いま有効な警報を一覧（読み取りのみ）

警報は avp_alerts と同じ口（journal 向けの stderr 行・jsonl・現在値）。docker・network・OpenSearch・
Temporal・DB には触れない。自分の heartbeat（``monitor-watch``）も書く。
終了コード: 0 異常なし / 1 stale・missing がある（unit が failed になり ``systemctl --user --failed`` に出る）。
``--show-alerts``: 0 警報なし / 1 crit あり / 3 warn のみ。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from avp_alerts import (  # noqa: E402
    Alert,
    AlertSink,
    current_alerts,
    default_dir,
    heartbeat_age,
    write_heartbeat,
)

# 既定の見張り対象と許容する古さ（秒）。timer の間隔（guard 30 秒・host monitor 60 秒）の 5〜10 倍
DEFAULT_COMPONENTS = {"opensearch-guard": 300, "host-process-monitor": 600}
SOURCE = "monitor-watch"


def check(directory: Path, components: dict[str, int], *, now: float) -> dict[str, Alert]:
    active: dict[str, Alert] = {}
    for name, max_age in sorted(components.items()):
        age = heartbeat_age(directory, name, now=now)
        if age is None:
            key = f"HEARTBEAT_MISSING:{name}"
            active[key] = Alert(
                key,
                "crit",
                f"{name} の heartbeat が無い・読めない（監視が一度も動いていない／壊れている）",
            )
        elif age > max_age:
            key = f"HEARTBEAT_STALE:{name}"
            active[key] = Alert(
                key,
                "crit",
                f"{name} の heartbeat が {int(age)}s 前（> {max_age}s）。監視が止まっている",
                {"age_s": int(age), "max_age_s": max_age},
            )
    return active


def show(directory: Path) -> int:
    worst = 0
    for source, alerts in sorted(current_alerts(directory).items()):
        for key, a in sorted(alerts.items()):
            print(f"{a['level']:4} {source} {key} {a['message']}")
            worst = 1 if a["level"] == "crit" else (worst or 3)
    return worst


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--state-dir", default=str(default_dir()))
    ap.add_argument("--component", action="append", default=[], metavar="NAME=MAX_AGE_S")
    ap.add_argument("--show-alerts", action="store_true")
    a = ap.parse_args(argv)
    directory = Path(a.state_dir)
    if a.show_alerts:
        return show(directory)
    components = dict(DEFAULT_COMPONENTS)
    for spec in a.component:
        name, _, age = spec.partition("=")
        if not age.isdigit():
            ap.error(f"--component は NAME=秒: {spec!r}")
        components[name] = int(age)
    clock = time.time() if now is None else now
    active = check(directory, components, now=clock)
    AlertSink(directory, now=clock).sync(SOURCE, active)
    write_heartbeat(directory, SOURCE, now=clock, level="crit" if active else "ok")
    return 1 if active else 0


if __name__ == "__main__":
    sys.exit(main())
