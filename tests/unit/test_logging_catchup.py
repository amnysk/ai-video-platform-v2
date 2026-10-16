"""deploy/logging/scripts/catchup.py: 位置 DB の offset と json-file の大きさの差（I-17）。

理由は docs/testing/logging-platform-rationale.md §7。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "logging" / "scripts" / "catchup.py"


@pytest.fixture(scope="module")
def catchup() -> ModuleType:
    spec = importlib.util.spec_from_file_location("avp_logging_catchup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass が自分の module を引く
    spec.loader.exec_module(module)
    return module


def _tail_db(path: Path) -> sqlite3.Connection:
    """Fluent Bit 5.1.2 の位置 DB と同じ表（使う列だけ）。WAL で開いたまま返す。"""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute(
        "CREATE TABLE in_tail_files (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " offset INTEGER, inode INTEGER, created INTEGER, rotated INTEGER DEFAULT 0)"
    )
    con.commit()
    return con


def _log(cdir: Path, name: str, size: int) -> Path:
    cdir.mkdir(parents=True, exist_ok=True)
    f = cdir / name
    f.write_bytes(b"x" * size)
    return f


def _track(con: sqlite3.Connection, f: Path, offset: int) -> None:
    con.execute(
        "INSERT INTO in_tail_files (name, offset, inode) VALUES (?, ?, ?)",
        (f"/containers/{f.parent.name}/{f.name}", offset, f.stat().st_ino),
    )
    con.commit()


def _main(catchup: ModuleType, capsys, *args: str) -> tuple[int, dict]:
    code = catchup.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_counts_unread_bytes_per_file_by_inode(catchup, tmp_path, capsys) -> None:
    containers = tmp_path / "containers"
    db = tmp_path / "tail.db"
    con = _tail_db(db)
    a = _log(containers / ("a" * 64), "a-json.log", 1000)
    rotated = _log(containers / ("a" * 64), "a-json.log.1", 500)
    b = _log(containers / ("b" * 64), "b-json.log", 300)
    _track(con, a, 1000)  # 読み終えた
    _track(con, rotated, 200)  # rotation 済みの続き（名前は変わっても inode で照合）
    _track(con, b, 0)

    code, out = _main(catchup, capsys, "--db", str(db), "--containers", str(containers))
    assert out["behind_bytes"] == 300 + 300
    assert out["files"] == 3 and out["files_behind"] == 2 and out["untracked"] == 0
    assert code == 0, "既定の許容差（1行の上限）以下"

    code, out = _main(
        catchup, capsys, "--db", str(db), "--containers", str(containers),
        "--max-behind-bytes", "599",
    )  # fmt: skip
    assert code == 1 and out["ok"] is False
    assert out["worst"]["offset"] in (0, 200)


def test_untracked_files_count_as_unread(catchup, tmp_path, capsys) -> None:
    """位置 DB に無いファイル（Collector が見つけていない・停止中に作られた）は全量が未読。"""
    containers = tmp_path / "containers"
    db = tmp_path / "tail.db"
    _tail_db(db)
    _log(containers / ("c" * 64), "c-json.log", 4096)
    code, out = _main(
        catchup, capsys, "--db", str(db), "--containers", str(containers),
        "--max-behind-bytes", "0",
    )  # fmt: skip
    assert code == 1
    assert out["behind_bytes"] == 4096 and out["untracked"] == 1
    assert out["worst"]["tracked"] is False


def test_only_the_given_container_ids(catchup, tmp_path, capsys) -> None:
    """収集対象 project のコンテナだけを見る（ログ基盤自身・別 project のファイルは数えない）。"""
    containers = tmp_path / "containers"
    db = tmp_path / "tail.db"
    con = _tail_db(db)
    target = _log(containers / ("d" * 64), "d-json.log", 100)
    _track(con, target, 100)
    _log(containers / ("e" * 64), "e-json.log", 9999)  # 対象外・未読
    code, out = _main(
        catchup, capsys, "--db", str(db), "--containers", str(containers),
        "--id", "d" * 64, "--id", "f" * 64, "--max-behind-bytes", "0",
    )  # fmt: skip
    assert code == 0 and out["behind_bytes"] == 0 and out["files"] == 1


def test_reads_a_copy_that_includes_the_wal(catchup, tmp_path, capsys) -> None:
    """Fluent Bit は位置 DB を排他的に開いたまま WAL に書く。DB だけを複製すると古い offset を読む。

    check-pipeline.sh は tail.db* を一時ディレクトリへ複製して渡す。
    WAL の内容が読めることを固定する。
    """
    live = tmp_path / "live"
    live.mkdir()
    containers = tmp_path / "containers"
    con = _tail_db(live / "tail.db")
    f = _log(containers / ("g" * 64), "g-json.log", 2048)
    _track(con, f, 2048)  # checkpoint されず WAL にだけある
    assert (live / "tail.db-wal").stat().st_size > 0

    copy = tmp_path / "copy"
    copy.mkdir()
    for p in live.glob("tail.db*"):
        shutil.copy2(p, copy / p.name)
    code, out = _main(
        catchup, capsys, "--db", str(copy / "tail.db"), "--containers", str(containers),
        "--max-behind-bytes", "0",
    )  # fmt: skip
    con.close()
    assert code == 0 and out["behind_bytes"] == 0 and out["untracked"] == 0


def test_unreadable_db_is_its_own_exit_code(catchup, tmp_path, capsys) -> None:
    (tmp_path / "tail.db").write_bytes(b"not a database")
    (tmp_path / "containers").mkdir()
    code, out = _main(
        catchup, capsys, "--db", str(tmp_path / "tail.db"), "--containers",
        str(tmp_path / "containers"),
    )  # fmt: skip
    assert code == 2 and out["ok"] is False
