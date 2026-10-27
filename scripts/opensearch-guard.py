#!/usr/bin/env python3
# ruff: noqa: E501  (全角文字を 2 幅で数えるため、日本語の説明文が 100 を超える)
"""OpenSearch のメモリ保護（hysteresis 付き）・安全な自動復旧・OpenSearch 非依存の警報（ADR-0059）。

``deploy/logging/scripts/check-pipeline.sh --enforce-memory``（ADR-0040 §7、1 サンプルで停止、復旧なし）の
置き換え。2026-10-21 00:02 の 1 サンプル（1039MiB）で OpenSearch が止まり 4 日間誰も気付かなかった事故が根拠。

    scripts/opensearch-guard.py [--auto-restart] [--dry-run] [--state-dir DIR] ...

1 回の実行 = 1 サンプル（systemd timer から 30 秒毎）。状態は ``guard-state.json``。
  停止  MemAvailable が 1.5GiB 未満の**連続 N サンプル（既定 5）かつ 180 秒以上**。Dashboards は 2GiB 未満で同様。
        サンプルの間が ``--max-sample-gap-s`` を超えたら連続とみなさない（止まっていた間の古い値で動かない）。
  復旧  ``--auto-restart`` を付けたときだけ。**guard が止めた** OpenSearch を ``docker start <その 1 コンテナ>`` で
        起こす。条件は全て: MemAvailable >= 4096MiB の連続 3 サンプル / avp2 の全コンテナが healthy /
        停止から 10 分以上 / 直近の試行からの backoff（base x 2^過去の試行回数）/ 24 時間の試行が上限未満。
        compose up・recreate・rm・volume には触れない。上限に達したら止めたまま警報（RESTART_CAPPED）。
  警報  OpenSearch・Temporal・DB に依存しない（scripts/avp_alerts.py: journal 向けの stderr 行・jsonl・現在値）。
        OpenSearch 停止 / 停止が長い / buffer が上限に近い（破棄の前）/ chunk が retry 窓に近い / 破棄の発生。
  heartbeat  毎回 ``heartbeat-opensearch-guard.json`` を書く（monitor-watch.py が古さを見る。INV-80）。

使う docker の動詞は ps / inspect / stop / start / logs だけ（テストが検査）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from avp_alerts import Alert, AlertSink, default_dir, write_heartbeat  # noqa: E402

# ---- 閾値（この表が定義の場所。docs/operations/opensearch-recovery.md と ADR-0059 が同じ数を書く）----
STOP_MEM_MIB = 1536  # ADR-0040 §7: これ未満が続いたら OpenSearch を止める
DASHBOARDS_STOP_MEM_MIB = 2048  # これ未満が続いたら Dashboards を止める
STOP_SAMPLES = 5
STOP_MIN_SPAN_S = 180
MAX_SAMPLE_GAP_S = 150
RESTART_MEM_MIB = (
    4096  # opensearch-recovery.md の復旧前提（mem_limit 2.5GiB + 停止線 1.5GiB の余裕）
)
RESTART_SAMPLES = 3
MIN_STOPPED_S = 600
RESTART_BACKOFF_S = 600
MAX_RESTARTS_24H = 3
STOPPED_ALERT_S = 1800
# Fluent Bit の設定（deploy/logging/fluent-bit/fluent-bit.yaml / compose.logging.yaml）と一致させる。
# 食い違いは tests/unit/test_opensearch_guard.py が検出する
FB_RETRY_LIMIT = 288
FB_SCHEDULER_CAP_S = 300
FB_MEAN_BACKOFF_S = 140  # 実測: 72 回で 2h49m（platform.md §7）= 約 141 秒/回
FB_STORAGE_LIMIT_BYTES = 1 << 30
BUFFER_WARN_RATIO, BUFFER_CRIT_RATIO = 0.70, 0.90
CHUNK_AGE_WARN_RATIO, CHUNK_AGE_CRIT_RATIO = 0.50, 0.80

DAY_S = 86400
SOURCE = "opensearch-guard"


class Docker:
    def __init__(self, exe: str) -> None:
        self.exe = exe

    def run(self, *args: str) -> tuple[int, str]:
        try:
            p = subprocess.run(  # noqa: S603
                [self.exe, *args], capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, str(e)
        return p.returncode, p.stdout + (p.stderr if p.returncode else "")


def _labels(project: str, service: str | None = None) -> list[str]:
    out = ["--filter", f"label=com.docker.compose.project={project}"]
    if service:
        out += ["--filter", f"label=com.docker.compose.service={service}"]
    return out


@dataclass
class Config:
    project: str
    target_project: str
    auto_restart: bool
    dry_run: bool
    state_dir: Path
    meminfo: Path
    buffer_dir: Path
    buffer_limit: int
    retry_window_s: int
    stop_samples: int
    stop_min_span_s: int
    max_gap_s: int
    min_stopped_s: int
    backoff_s: int
    max_restarts: int
    stopped_alert_s: int


def read_mem_available_mib(path: Path) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True))
        tmp.replace(path)
    except OSError:
        pass


def trailing_run(samples: list[dict[str, float]], pred: Any) -> list[dict[str, float]]:
    run: list[dict[str, float]] = []
    for s in reversed(samples):
        if not pred(s["mib"]):
            break
        run.append(s)
    run.reverse()
    return run


def sustained_low(samples: list[dict[str, float]], line: int, n: int, span_s: int) -> bool:
    run = trailing_run(samples, lambda m: m < line)
    return len(run) >= n and run[-1]["t"] - run[0]["t"] >= span_s


def workers_healthy(rows: list[tuple[str, str]]) -> tuple[bool, list[str]]:
    """avp2 の全コンテナが健全か。行が 1 つも無い（確認できない）は不健全として扱う。"""
    bad: list[str] = []
    for name, status in rows:
        up = (
            status.startswith("Up")
            and "unhealthy" not in status
            and "health: starting" not in status
        )
        one_shot_done = status.startswith("Exited (0)")
        if not (up or one_shot_done):
            bad.append(f"{name}: {status}")
    if not rows:
        bad.append("avp2 のコンテナを確認できない")
    return not bad, bad


def _inspect_state(dk: Docker, cid: str) -> dict[str, Any] | None:
    rc, out = dk.run("inspect", "-f", "{{json .State}}", cid)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _find(dk: Docker, project: str, service: str) -> tuple[str | None, bool]:
    """(コンテナ id, docker に聞けたか)。"""
    rc, out = dk.run("ps", "-a", "-q", "--no-trunc", *_labels(project, service))
    if rc != 0:
        return None, False
    ids = out.split()
    return (ids[0] if ids else None), True


def _parse_iso(value: str) -> float | None:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", value or "")
    if not m or value.startswith("0001"):
        return None
    import calendar

    return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))  # type: ignore[arg-type]


def buffer_usage(buffer_dir: Path, now: float) -> tuple[int, float | None]:
    """(合計 bytes, 最も古い chunk の経過秒)。読めなければ (0, None)。"""
    total = 0
    oldest: float | None = None
    for root, _dirs, files in os.walk(buffer_dir):
        for f in files:
            try:
                st = (Path(root) / f).stat()
            except OSError:
                continue
            total += st.st_size
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
    return total, (None if oldest is None else max(0.0, now - oldest))


def _fluent_bit_alerts(
    cfg: Config, dk: Docker, state: dict[str, Any], now: float, active: dict[str, Alert]
) -> None:
    fb, asked = _find(dk, cfg.project, "fluent-bit")
    running = False
    if fb:
        st = _inspect_state(dk, fb)
        running = bool(st and st.get("Status") == "running")
    if asked and not running:
        active["FLUENT_BIT_NOT_RUNNING"] = Alert(
            "FLUENT_BIT_NOT_RUNNING", "crit", "Fluent Bit が動いていない（ログを収集していない）"
        )
    total, oldest_age = buffer_usage(cfg.buffer_dir, now)
    ratio = total / cfg.buffer_limit if cfg.buffer_limit else 0.0
    if ratio >= BUFFER_WARN_RATIO:
        level = "crit" if ratio >= BUFFER_CRIT_RATIO else "warn"
        active["FLUENT_BIT_BUFFER_NEAR_LIMIT"] = Alert(
            "FLUENT_BIT_BUFFER_NEAR_LIMIT",
            level,
            f"Fluent Bit の filesystem buffer が上限の {ratio:.0%}（{total} / {cfg.buffer_limit} bytes）。"
            "上限を超えると古い chunk から破棄される",
            {"bytes": total, "limit": cfg.buffer_limit},
        )
    if oldest_age is not None and cfg.retry_window_s:
        aratio = oldest_age / cfg.retry_window_s
        if aratio >= CHUNK_AGE_WARN_RATIO:
            level = "crit" if aratio >= CHUNK_AGE_CRIT_RATIO else "warn"
            active["FLUENT_BIT_CHUNK_AGE"] = Alert(
                "FLUENT_BIT_CHUNK_AGE",
                level,
                f"最も古い chunk が retry 窓（約 {cfg.retry_window_s}s）の {aratio:.0%}。"
                "超えると retry 上限で破棄される",
                {"oldest_age_s": int(oldest_age), "retry_window_s": cfg.retry_window_s},
            )
    # 破棄（retry 上限）。ログの 'chunk ... cannot be retried'。chunk id で重複を数えない
    seen: dict[str, float] = {k: float(v) for k, v in state.get("fb_dropped", {}).items()}
    if fb and running:
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(state.get("fb_scan_at", now - 600)))
        )
        rc, out = dk.run("logs", "--since", since, fb)
        if rc == 0:
            for line in out.splitlines():
                if "cannot be retried" in line:
                    m = re.search(r"chunk '([^']+)'", line)
                    seen.setdefault(m.group(1) if m else f"line-{hash(line)}", now)
            state["fb_scan_at"] = now
    seen = {k: t for k, t in seen.items() if now - t < DAY_S}
    state["fb_dropped"] = seen
    if seen:
        active["FLUENT_BIT_DROPPED"] = Alert(
            "FLUENT_BIT_DROPPED",
            "crit",
            f"Fluent Bit が retry 上限で chunk を破棄した（直近 24 時間 {len(seen)} 件）。"
            "json-file が残っていれば scripts/log-recoverability.py で範囲を確認する",
            {"dropped_chunks_24h": len(seen)},
        )


def run_once(cfg: Config, dk: Docker, now: float) -> int:
    state_path = cfg.state_dir / "guard-state.json"
    state = _load_state(state_path)
    active: dict[str, Alert] = {}
    samples: list[dict[str, float]] = list(state.get("samples", []))
    attempts = [float(t) for t in state.get("restart_attempts", []) if now - float(t) < DAY_S]

    mib = read_mem_available_mib(cfg.meminfo)
    if mib is None:
        active["MEMINFO_UNREADABLE"] = Alert(
            "MEMINFO_UNREADABLE", "warn", "MemAvailable を読めない（止める・起こす判断をしない）"
        )
    else:
        if samples and now - samples[-1]["t"] > cfg.max_gap_s:
            samples = []  # 監視が止まっていた間の古い値を連続に数えない
        samples.append({"t": now, "mib": float(mib)})
        samples = samples[-40:]

    os_id, asked = _find(dk, cfg.project, "opensearch")
    if not asked:
        active["DOCKER_UNAVAILABLE"] = Alert(
            "DOCKER_UNAVAILABLE",
            "crit",
            "docker に問い合わせられない（止める・起こす判断をしない）",
        )
    os_state = _inspect_state(dk, os_id) if os_id else None
    os_running = bool(os_state and os_state.get("Status") == "running")
    rec: dict[str, Any] | None = state.get("stopped")

    if mib is not None and mib < STOP_MEM_MIB and not (rec and not os_running):
        low = trailing_run(samples, lambda m: m < STOP_MEM_MIB)
        active["MEMORY_LOW"] = Alert(
            "MEMORY_LOW",
            "warn",
            f"MemAvailable {mib}MiB < {STOP_MEM_MIB}MiB（{len(low)}/{cfg.stop_samples} サンプル。"
            "一時的な低下なら止めない）",
            {"mib": mib, "consecutive": len(low)},
        )

    if asked and os_id is None:
        active["OPENSEARCH_MISSING"] = Alert(
            "OPENSEARCH_MISSING", "crit", f"{cfg.project} の opensearch コンテナが無い"
        )
    elif asked and os_running:
        if rec:  # 起動した（guard または人）。記録を畳む。試行回数は 24 時間残す（flap guard）
            state["last_recovered_at"] = now
            rec = None
        if mib is not None and sustained_low(
            samples, STOP_MEM_MIB, cfg.stop_samples, cfg.stop_min_span_s
        ):
            rec = _stop_for_pressure(cfg, dk, now, samples, mib, str(os_id), active)
        elif mib is not None and sustained_low(
            samples, DASHBOARDS_STOP_MEM_MIB, cfg.stop_samples, cfg.stop_min_span_s
        ):
            dash, _ = _find(dk, cfg.project, "dashboards")
            if dash and not cfg.dry_run:
                dst = _inspect_state(dk, dash)
                if dst and dst.get("Status") == "running" and dk.run("stop", dash)[0] == 0:
                    state["dashboards_stopped_at"] = now
    elif asked and os_id:
        if rec is None:
            finished = _parse_iso(str((os_state or {}).get("FinishedAt", "")))
            since = f"{int(now - finished)}s 前から" if finished else "時刻不明"
            active["OPENSEARCH_DOWN_UNMANAGED"] = Alert(
                "OPENSEARCH_DOWN_UNMANAGED",
                "crit",
                f"OpenSearch が止まっている（{since}）。guard が止めたものではないので自動では起こさない",
                {"finished_at": (os_state or {}).get("FinishedAt")},
            )
        else:
            _handle_stopped(cfg, dk, now, samples, attempts, rec, os_id, active)

    state["stopped"] = rec
    state["samples"] = samples
    state["restart_attempts"] = attempts
    _fluent_bit_alerts(cfg, dk, state, now, active)

    sink = AlertSink(cfg.state_dir, now=now)
    sink.sync(SOURCE, active)
    level = (
        "crit"
        if any(a.level == "crit" for a in active.values())
        else ("warn" if any(a.level == "warn" for a in active.values()) else "ok")
    )
    write_heartbeat(
        cfg.state_dir, SOURCE, now=now, level=level, mem_mib=mib, opensearch_running=os_running
    )
    _save_state(state_path, state)
    return 1 if level == "crit" else 0


def _stop_for_pressure(
    cfg: Config,
    dk: Docker,
    now: float,
    samples: list[dict[str, float]],
    mib: int,
    os_id: str,
    active: dict[str, Alert],
) -> dict[str, Any] | None:
    if cfg.dry_run:
        active["MEMORY_SUSTAINED_LOW"] = Alert(
            "MEMORY_SUSTAINED_LOW",
            "crit",
            f"[dry-run] 持続的な低下（{mib}MiB）。実行なら OpenSearch を止める",
        )
        return None
    dash, _ = _find(dk, cfg.project, "dashboards")
    if dash:
        dst = _inspect_state(dk, dash)
        if dst and dst.get("Status") == "running":
            dk.run("stop", dash)
    if dk.run("stop", os_id)[0] != 0:
        active["OPENSEARCH_STOP_FAILED"] = Alert(
            "OPENSEARCH_STOP_FAILED", "crit", "メモリ逼迫で OpenSearch を止めようとして失敗した"
        )
        return None
    rec = {
        "reason": (
            f"MemAvailable {mib}MiB < {STOP_MEM_MIB}MiB が連続 {cfg.stop_samples} サンプル・"
            f"{cfg.stop_min_span_s} 秒以上続いた（ADR-0059）"
        ),
        "stopped_at": now,
        "container": os_id,
        "samples": samples[-10:],
        "resume_conditions": {
            "mem_available_mib": RESTART_MEM_MIB,
            "consecutive_samples": RESTART_SAMPLES,
            "workers_healthy": True,
            "min_stopped_s": cfg.min_stopped_s,
            "max_restarts_24h": cfg.max_restarts,
            "scope": "opensearch container only",
        },
    }
    return rec


def _handle_stopped(
    cfg: Config,
    dk: Docker,
    now: float,
    samples: list[dict[str, float]],
    attempts: list[float],
    rec: dict[str, Any],
    os_id: str,
    active: dict[str, Alert],
) -> None:
    age = now - float(rec["stopped_at"])
    active["OPENSEARCH_STOPPED"] = Alert(
        "OPENSEARCH_STOPPED",
        "crit",
        f"OpenSearch を guard が停止中（{int(age)}s）。理由: {rec['reason']}",
        {"stopped_at": rec["stopped_at"], "restart_attempts_24h": len(attempts)},
    )
    if age >= cfg.stopped_alert_s:
        active["OPENSEARCH_STOPPED_LONG"] = Alert(
            "OPENSEARCH_STOPPED_LONG",
            "crit",
            f"OpenSearch の停止が {int(age)}s（>= {cfg.stopped_alert_s}s）。Fluent Bit の buffer と retry 窓を確認する",
        )
    if not cfg.auto_restart:
        return
    if len(attempts) >= cfg.max_restarts:
        active["RESTART_CAPPED"] = Alert(
            "RESTART_CAPPED",
            "crit",
            f"24 時間の自動起動が上限（{cfg.max_restarts} 回）。止めたまま。原因を調べて手で起動する"
            "（docs/operations/opensearch-recovery.md）",
            {"restart_attempts_24h": len(attempts)},
        )
        return
    high = trailing_run(samples, lambda m: m >= RESTART_MEM_MIB)
    mem_ok = len(high) >= RESTART_SAMPLES
    wait_ok = age >= cfg.min_stopped_s and (
        not attempts or now - attempts[-1] >= cfg.backoff_s * (2 ** len(attempts))
    )
    if not (mem_ok and wait_ok):
        return
    rc, out = dk.run(
        "ps",
        "-a",
        "--no-trunc",
        "--format",
        "{{.Names}}\t{{.Status}}",
        *_labels(cfg.target_project),
    )
    rows = (
        [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line] if rc == 0 else []
    )
    healthy, bad = workers_healthy([(a, b) for a, b in rows])
    if not healthy:
        active["RESTART_BLOCKED"] = Alert(
            "RESTART_BLOCKED",
            "warn",
            "復旧条件のうちメモリは満たしたが、他のコンテナが健全でないので起動しない: "
            + "; ".join(bad[:5]),
        )
        return
    if cfg.dry_run:
        return
    attempts.append(now)  # 起動に失敗しても 1 回に数える（flap guard が緩まない）
    if dk.run("start", os_id)[0] != 0:
        active["OPENSEARCH_START_FAILED"] = Alert(
            "OPENSEARCH_START_FAILED", "crit", "OpenSearch の docker start に失敗した"
        )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--project", default="avp2-logging")
    ap.add_argument("--target-project", default="avp2")
    ap.add_argument("--docker", default="docker")
    ap.add_argument("--meminfo", default="/proc/meminfo")
    ap.add_argument("--state-dir", default=str(default_dir()))
    ap.add_argument(
        "--buffer-dir",
        default=str(Path.home() / ".local/share/docker/volumes/avp2-logging_fbstate/_data/buffer"),
    )
    ap.add_argument("--buffer-limit-bytes", type=int, default=FB_STORAGE_LIMIT_BYTES)
    ap.add_argument(
        "--retry-window-s", type=int, default=FB_RETRY_LIMIT * FB_MEAN_BACKOFF_S,
        help="Fluent Bit が chunk を諦めるまでの目安（retry_limit x 平均 backoff）",
    )  # fmt: skip
    ap.add_argument("--auto-restart", action="store_true", help="opt-in。既定は止めるだけ")
    ap.add_argument("--dry-run", action="store_true", help="docker の変更をしない")
    ap.add_argument("--stop-samples", type=int, default=STOP_SAMPLES)
    ap.add_argument("--stop-min-span-s", type=int, default=STOP_MIN_SPAN_S)
    ap.add_argument("--max-sample-gap-s", type=int, default=MAX_SAMPLE_GAP_S)
    ap.add_argument("--min-stopped-s", type=int, default=MIN_STOPPED_S)
    ap.add_argument("--restart-backoff-s", type=int, default=RESTART_BACKOFF_S)
    ap.add_argument("--max-restarts-24h", type=int, default=MAX_RESTARTS_24H)
    ap.add_argument("--stopped-alert-s", type=int, default=STOPPED_ALERT_S)
    return ap


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.project in ("avp2", a.target_project):
        ap.error(f"refusing: --project {a.project} はアプリの project（止める対象にしない）")
    cfg = Config(
        project=a.project,
        target_project=a.target_project,
        auto_restart=a.auto_restart,
        dry_run=a.dry_run,
        state_dir=Path(a.state_dir),
        meminfo=Path(a.meminfo),
        buffer_dir=Path(a.buffer_dir),
        buffer_limit=a.buffer_limit_bytes,
        retry_window_s=a.retry_window_s,
        stop_samples=a.stop_samples,
        stop_min_span_s=a.stop_min_span_s,
        max_gap_s=a.max_sample_gap_s,
        min_stopped_s=a.min_stopped_s,
        backoff_s=a.restart_backoff_s,
        max_restarts=a.max_restarts_24h,
        stopped_alert_s=a.stopped_alert_s,
    )
    return run_once(cfg, Docker(a.docker), time.time() if now is None else now)


if __name__ == "__main__":
    sys.exit(main())
