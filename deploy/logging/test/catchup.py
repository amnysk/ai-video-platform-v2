"""Collector の追いつき確認: Fluent Bit の位置 DB（tail.db）の offset と json-file のサイズの差。

ADR-0040 §5 / runbook §7（deploy 前に未読が残っていないこと）。B の check-pipeline.sh には
（2026-10-06 時点で）無いので、試験と runbook の手順のために置く。tail.db はホストへ複製して読む
（Fluent Bit が書いている DB を直接開かない）。

    catchup.py --db /path/to/tail.db --containers ~/.local/share/docker/containers \
        [--project avp2-oslog-c]   # 対象 project のコンテナのファイルだけ見る
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--containers", required=True)
    ap.add_argument("--project", help="config.v2.json の compose project label で絞る")
    ap.add_argument("--max-behind-bytes", type=int, default=0)
    args = ap.parse_args(argv)

    rows = (
        sqlite3.connect(args.db).execute("SELECT name, offset, inode FROM in_tail_files").fetchall()
    )
    by_inode = {inode: (name, offset) for name, offset, inode in rows}
    behind_total = 0
    unread: list[dict] = []
    for cdir in Path(args.containers).iterdir():
        if args.project:
            try:
                cfg = json.loads((cdir / "config.v2.json").read_text())
            except OSError:
                continue
            labels = cfg.get("Config", {}).get("Labels", {}) or {}
            if labels.get("com.docker.compose.project") != args.project:
                continue
        for f in cdir.glob("*-json.log*"):
            st = f.stat()
            name, offset = by_inode.get(st.st_ino, (None, 0))
            behind = st.st_size - offset
            if behind > 0:
                behind_total += behind
                unread.append(
                    {
                        "file": f.name,
                        "size": st.st_size,
                        "offset": offset,
                        "tracked": name is not None,
                    }
                )
    ok = behind_total <= args.max_behind_bytes
    print(json.dumps({"ok": ok, "behind_bytes": behind_total, "files_behind": unread[:20]}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
