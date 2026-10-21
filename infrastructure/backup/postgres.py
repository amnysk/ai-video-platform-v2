"""本番 PostgreSQL コンテナへの読み取り専用の操作（``docker exec`` で pg_dump / SELECT だけ）。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from infrastructure.backup.runner import CmdResult, CommandRunner, FileResult

#: 全 user table の行数を 1 本の SELECT で数える（書き込みなし）
ROW_COUNT_SQL = (
    "SELECT format('%I.%I', table_schema, table_name), "
    "(xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', "
    "table_schema, table_name), false, true, '')))[1]::text::bigint "
    "FROM information_schema.tables "
    "WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') "
    "ORDER BY 1"
)


class CommandFailed(Exception):
    pass


def parse_pipe_rows(out: str, sep: str = "|") -> list[list[str]]:
    return [line.split(sep) for line in out.splitlines() if line.strip()]


#: psql は SELECT だけを送る。接続設定でも書き込めないようにする（読み取り専用は慣習にしない）
READ_ONLY_PGOPTIONS = "PGOPTIONS=-c default_transaction_read_only=on"


class PostgresAccess:
    def __init__(self, runner: CommandRunner, container: str, user: str) -> None:
        self._r = runner
        self._container = container
        self._user = user

    def _exec(self, args: Sequence[str], *, stdin: bool = False) -> list[str]:
        return ["docker", "exec", *(["-i"] if stdin else []), self._container, *args]

    def query_rows(self, db: str, sql: str, sep: str = "\x1f") -> list[list[str]]:
        """SELECT の結果を列区切り ``sep`` で読む（key に ``|`` が在っても壊れない）。"""
        return parse_pipe_rows(self._psql(db, sql, sep), sep)

    def _psql(self, db: str, sql: str, sep: str = "|") -> str:
        res = self._r.run(
            [
                "docker",
                "exec",
                "-e",
                READ_ONLY_PGOPTIONS,
                self._container,
                "psql",
                "-U",
                self._user,
                "-d",
                db,
                "-v",
                "ON_ERROR_STOP=1",
                "-F",
                sep,
                "-Atc",
                sql,
            ]
        )
        if res.returncode != 0:
            raise CommandFailed(f"psql {db}: rc={res.returncode} {res.stderr.strip()[:200]}")
        return res.stdout

    def row_counts(self, db: str) -> dict[str, int]:
        return {row[0]: int(row[1]) for row in parse_pipe_rows(self._psql(db, ROW_COUNT_SQL))}

    def alembic_revision(self, db: str) -> str:
        rows = [
            r[0] for r in parse_pipe_rows(self._psql(db, "SELECT version_num FROM alembic_version"))
        ]
        if len(rows) != 1:
            raise CommandFailed(f"alembic_version has {len(rows)} rows")
        return rows[0]

    def version(self) -> str:
        res = self._r.run(self._exec(["pg_dump", "--version"]))
        if res.returncode != 0:
            raise CommandFailed("pg_dump --version failed")
        return res.stdout.strip()

    def dump(self, db: str, dest: Path) -> FileResult:
        return self._r.run_to_file(
            self._exec(["pg_dump", "-U", self._user, "-d", db, "--format=custom", "--no-password"]),
            dest,
        )

    def dump_globals(self, dest: Path) -> FileResult:
        return self._r.run_to_file(
            self._exec(["pg_dumpall", "-U", self._user, "--globals-only", "--no-password"]), dest
        )

    def list_toc(self, dump: Path) -> CmdResult:
        """dump の目次が読めること（構造が壊れていないこと）の確認。復元はしない。"""
        return self._r.run(self._exec(["pg_restore", "--list"], stdin=True), stdin_path=dump)
