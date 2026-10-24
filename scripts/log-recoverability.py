#!/usr/bin/env python3
# ruff: noqa: E501  (全角文字を 2 幅で数えるため、日本語の説明文が 100 を超える)
"""停止期間のログが **どこまで取り戻せるか** を報告し、backfill 用のファイルを作る（読み取り専用）。

OpenSearch / Fluent Bit が止まっていた期間（窓）について、project の各コンテナの json-file ログ
（``<containers-dir>/<id>/<id>-json.log*``）が窓のどこまで残っているかを調べる。

    scripts/log-recoverability.py --since 2026-10-20T15:02:00Z --until 2026-10-25T00:00:00Z \\
        [--project avp2] [--containers-dir DIR] [--out DIR] [--no-backfill] [--json]

コンテナごとに:
  recoverable    窓のうち json-file にまだ残っている範囲（rotation で消えていない）
  unrecoverable  窓のうち json-file からも消えた範囲（Fluent Bit の buffer にも無ければ戻せない）
  ※ コンテナが窓の途中で作られたなら、窓の開始はその作成時刻（作る前のログは存在しない）。
  ※ すでに削除されたコンテナは docker から見えないので対象外（ログも一緒に消えている）。
``--out`` を与えると、窓の中の行だけを **そのまま** ``<out>/<container>.jsonl`` へ複写する（0600）。
元の json-file には書かない。実際の再投入は所有者の承認が必要（docs/operations/opensearch-recovery.md §6）。
docker は ``ps`` と ``inspect`` だけ。network・OpenSearch・Fluent Bit には触れない。
終了コード: 0 全て取り戻せる / 1 取り戻せない範囲がある / 2 引数・読み取りの誤り
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_time(value: str) -> float | None:
    """RFC3339（ナノ秒まで）を epoch 秒に。読めなければ None。"""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    if "." in v:
        head, _, rest = v.partition(".")
        digits = ""
        for ch in rest:
            if not ch.isdigit():
                break
            digits += ch
        v = head + "." + digits[:6].ljust(6, "0") + rest[len(digits) :]
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ContainerReport:
    name: str
    container_id: str
    window: tuple[float, float]
    retained: tuple[float, float] | None = None
    lines_in_window: int = 0
    bad_lines: int = 0
    files: list[str] = field(default_factory=list)
    recoverable: tuple[float, float] | None = None
    unrecoverable: list[tuple[float, float]] = field(default_factory=list)
    backfill_file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        def rng(r: tuple[float, float] | None) -> dict[str, str] | None:
            return None if r is None else {"from": fmt(r[0]), "to": fmt(r[1])}

        return {
            "container": self.name,
            "id": self.container_id[:12],
            "window": rng(self.window),
            "retained": rng(self.retained),
            "recoverable": rng(self.recoverable),
            "unrecoverable": [rng(r) for r in self.unrecoverable],
            "lines_in_window": self.lines_in_window,
            "bad_lines": self.bad_lines,
            "files": self.files,
            "backfill_file": self.backfill_file,
        }


def _log_files(cdir: Path, cid: str) -> list[Path]:
    """古い順（.log.3 .. .log.1 .log）。"""
    found = sorted(
        (p for p in cdir.glob(f"{cid}-json.log*") if p.is_file()),
        key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0,
    )
    return found


def analyse(
    name: str,
    cid: str,
    created: float | None,
    since: float,
    until: float,
    containers_dir: Path,
    out_file: Path | None,
) -> ContainerReport:
    start = max(since, created) if created else since
    rep = ContainerReport(name, cid, (start, until))
    files = _log_files(containers_dir / cid, cid)
    rep.files = [f.name for f in files]
    first: float | None = None
    last: float | None = None
    kept: list[bytes] = []
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        for line in raw.splitlines():
            try:
                ts = parse_time(str(json.loads(line)["time"]))
            except (ValueError, KeyError, TypeError):
                ts = None
            if ts is None:
                rep.bad_lines += 1
                continue
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
            if start <= ts <= until:
                rep.lines_in_window += 1
                kept.append(line)
    if first is not None and last is not None:
        rep.retained = (first, last)
        lo, hi = max(start, first), min(until, last)
        if lo <= hi:
            rep.recoverable = (lo, hi)
        if first > start:  # 窓の先頭側が rotation で消えた（または元から無い）
            rep.unrecoverable.append((start, min(first, until)))
    else:
        rep.unrecoverable.append((start, until))  # 1 行も残っていない
    if out_file is not None and kept:
        out_file.write_bytes(b"\n".join(kept) + b"\n")
        out_file.chmod(0o600)
        rep.backfill_file = str(out_file)
    return rep


def _docker(exe: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    return p.returncode, p.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--since", required=True, help="窓の開始（RFC3339 UTC）")
    ap.add_argument("--until", required=True, help="窓の終了（RFC3339 UTC）")
    ap.add_argument("--project", default="avp2")
    ap.add_argument("--docker", default="docker")
    ap.add_argument(
        "--containers-dir",
        default=os.environ.get("AVP_LOG_CONTAINERS_DIR")
        or str(Path.home() / ".local/share/docker/containers"),
    )
    ap.add_argument("--out", default=None, help="backfill 用の複写先（無ければ報告だけ）")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    since, until = parse_time(a.since), parse_time(a.until)
    if since is None or until is None or since >= until:
        print("--since / --until が不正（RFC3339 で since < until）", file=sys.stderr)
        return 2
    cdir = Path(a.containers_dir)
    out_dir = None if (a.out is None or a.no_backfill) else Path(a.out)
    if out_dir is not None:
        resolved = out_dir.resolve()
        if resolved == cdir.resolve() or cdir.resolve() in resolved.parents:
            print("--out は containers-dir の外にする（元のログに書かない）", file=sys.stderr)
            return 2
        out_dir.mkdir(parents=True, exist_ok=True)
        out_dir.chmod(0o700)
    rc, listing = _docker(
        a.docker,
        "ps",
        "-a",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={a.project}",
        "--format",
        "{{.ID}}\t{{.Names}}",
    )
    if rc != 0:
        print("docker ps に失敗（対象コンテナを列挙できない）", file=sys.stderr)
        return 2
    reports: list[ContainerReport] = []
    for row in listing.splitlines():
        cid, _, name = row.partition("\t")
        if not cid or not name:
            continue
        rc2, created_raw = _docker(a.docker, "inspect", "-f", "{{.Created}}", cid)
        created = parse_time(created_raw) if rc2 == 0 else None
        reports.append(
            analyse(
                name,
                cid,
                created,
                since,
                until,
                cdir,
                None if out_dir is None else out_dir / f"{name}.jsonl",
            )
        )
    reports.sort(key=lambda r: r.name)
    if out_dir is not None:
        manifest = {
            "window": {"from": fmt(since), "to": fmt(until)},
            "containers": [r.as_dict() for r in reports],
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        (out_dir / "manifest.json").chmod(0o600)
    if a.json:
        print(json.dumps([r.as_dict() for r in reports], ensure_ascii=False, indent=2))
    else:
        print(f"window {fmt(since)} .. {fmt(until)}  project={a.project}")
        for r in reports:
            rec = (
                "none"
                if r.recoverable is None
                else f"{fmt(r.recoverable[0])} .. {fmt(r.recoverable[1])}"
            )
            lost = ", ".join(f"{fmt(x)} .. {fmt(y)}" for x, y in r.unrecoverable) or "-"
            print(
                f"{r.name}\n  recoverable   {rec}  ({r.lines_in_window} lines)\n  unrecoverable {lost}"
            )
    return 1 if any(r.unrecoverable for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
