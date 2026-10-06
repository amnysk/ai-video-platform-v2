"""Collector の追いつき: Fluent Bit の位置 DB（tail.db）の offset と json-file の大きさの差。

I-17。ホストで動かす（標準ライブラリだけ）。check-pipeline.sh が位置 DB を WAL ごと
一時ディレクトリへ複製してから呼ぶ（Fluent Bit は ``db.locking`` で DB を排他的に開いているので、
元の DB は開かない・触れない）。

    catchup.py --db <複製した tail.db> --containers <containers/> [--id <container id> ...]
        [--max-behind-bytes 262144]

``--id`` を渡すとそのコンテナのディレクトリだけを見る（収集対象 project のコンテナ。
``docker ps -a --no-trunc -q --filter label=com.docker.compose.project=<project>``）。
位置 DB の行は inode で照合する（rotation で名前が変わっても inode は同じ）。位置 DB に無い
ファイルは offset 0（未読）として数える。

標準出力に JSON を1行: ``{"ok", "behind_bytes", "files", "files_behind", "untracked", "worst"}``。
終了コード: 0 = 差が上限以下、1 = 上限超え、2 = 位置 DB を読めない。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

#: 既定の許容差。Collector の行の上限（COLLECTOR_LINE_MAX_BYTES）と同じ大きさ: 1行を書いている途中の
#: ファイルは差が出るが、それより大きい差は読めていない
DEFAULT_MAX_BEHIND_BYTES = 262144


@dataclass(frozen=True)
class FileLag:
    path: str
    size: int
    offset: int
    tracked: bool

    @property
    def behind(self) -> int:
        return max(self.size - self.offset, 0)


def read_offsets(db: Path) -> dict[int, int]:
    """位置 DB の inode -> offset。複製した DB を読み取り専用で開く。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT inode, offset FROM in_tail_files").fetchall()
    finally:
        con.close()
    return {int(inode): int(offset) for inode, offset in rows}


def file_lags(containers: Path, offsets: dict[int, int], ids: list[str] | None) -> list[FileLag]:
    dirs = [containers / i for i in ids] if ids else sorted(containers.iterdir())
    lags: list[FileLag] = []
    for cdir in dirs:
        if not cdir.is_dir():
            continue
        for f in sorted(cdir.glob("*-json.log*")):
            try:
                st = f.stat()
            except OSError:
                continue  # rotation で消えた
            offset = offsets.get(st.st_ino)
            lags.append(
                FileLag(
                    path=f"{cdir.name[:12]}/{f.name}",
                    size=st.st_size,
                    offset=offset or 0,
                    tracked=offset is not None,
                )
            )
    return lags


def summarize(lags: list[FileLag], max_behind: int) -> dict[str, object]:
    behind = [x for x in lags if x.behind > 0]
    total = sum(x.behind for x in behind)
    worst = max(behind, key=lambda x: x.behind, default=None)
    return {
        "ok": total <= max_behind,
        "behind_bytes": total,
        "files": len(lags),
        "files_behind": len(behind),
        "untracked": sum(1 for x in lags if not x.tracked),
        "worst": None
        if worst is None
        else {
            "file": worst.path,
            "size": worst.size,
            "offset": worst.offset,
            "tracked": worst.tracked,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--containers", required=True, type=Path)
    ap.add_argument("--id", action="append", dest="ids", help="見るコンテナ ID（複数可）")
    ap.add_argument("--max-behind-bytes", type=int, default=DEFAULT_MAX_BEHIND_BYTES)
    args = ap.parse_args(argv)
    try:
        offsets = read_offsets(args.db)
    except sqlite3.Error as exc:
        print(json.dumps({"ok": False, "error": f"position db: {exc}"}))
        return 2
    result = summarize(file_lags(args.containers, offsets, args.ids), args.max_behind_bytes)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
