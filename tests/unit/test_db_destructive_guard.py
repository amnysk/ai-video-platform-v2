"""破壊的 DB 操作の単一ガード（INV-69 / INV-83 / ADR-0055）の検査。

静的（URL）検査は **接続を作る前** に拒否し、動的検査は **実際に繋がっている接続** の
``current_database()`` と印で拒否する。後者が無いと ``connect_args`` / ``creator`` で URL と違う所へ
繋いだだけで素通りする。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from infrastructure.db import safety
from infrastructure.db.safety import (
    TEST_DB_MARKER,
    UnsafeDatabaseError,
    assert_destructive_allowed,
    check_live_identity,
    check_statement,
    guarded_create_all,
    guarded_drop_all,
    guarded_drop_database,
    validate_test_database_url,
)

PROD = "postgresql+psycopg://avp:pw@localhost:5432/avp"
TEST = "postgresql+psycopg://avp:pw@localhost:5432/avp_test"
CLEAN_ENV: dict[str, str] = {}


class _Metadata:
    """``drop_all`` / ``create_all`` が呼ばれたかを記録する。呼ばれたら DDL が走ったということ。"""

    def __init__(self) -> None:
        self.ddl: list[str] = []

    def drop_all(self, connection: Any) -> None:
        self.ddl.append("drop_all")

    def create_all(self, connection: Any) -> None:
        self.ddl.append("create_all")


class _Result:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def one(self) -> tuple[Any, ...]:
        return self._row


class _Conn:
    """URL（``engine.url``）と **実際の接続先**（``live``）を別々に持つ偽の接続。"""

    def __init__(self, url: str, live: tuple[str | None, str | None]) -> None:
        self.engine = SimpleNamespace(url=make_url(url))
        self._live = live
        self.statements: list[str] = []

    def execute(self, statement: Any, *a: Any, **k: Any) -> Any:
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("DROP"):
            raise AssertionError(f"guard let a destructive statement run: {sql}")
        return _Result(self._live)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", PROD)
    monkeypatch.delenv("AVP_ALLOW_REMOTE_TEST_DB", raising=False)
    for key in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE", "PGDATABASE"):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------------ 静的検査


@pytest.mark.parametrize(
    "url",
    [
        PROD,
        "postgresql+psycopg://avp:pw@localhost:5432/temporal",
        "postgresql+psycopg://avp:pw@localhost:5432/temporal_visibility",
        "postgresql+psycopg://avp:pw@localhost:5432/postgres",
        "postgresql+psycopg://avp:pw@localhost:5432/AVP",
        "postgresql+psycopg://avp:pw@localhost:5432/avp?options=-csearch_path%3Davp_test",
        "postgresql+psycopg://avp:pw@127.0.0.1:5432/avp",
        "postgresql+psycopg://avp:pw@postgres:5432/avp",
        "postgresql+psycopg://avp:pw@db.prod.example.com:5432/avp",
    ],
)
def test_protected_database_is_refused_before_any_connection(url: str) -> None:
    conn = _Conn(url, live=("avp_test", TEST_DB_MARKER))
    meta = _Metadata()
    with pytest.raises(UnsafeDatabaseError):
        guarded_drop_all(conn, meta)
    with pytest.raises(UnsafeDatabaseError):
        guarded_create_all(conn, meta)
    assert conn.statements == [], "静的検査が接続より前に効いていない"
    assert meta.ddl == []


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///avp_test",  # host 無し: libpq が PGHOST / PGSERVICE で決める
        "postgresql+psycopg://avp:pw@/avp_test",
        "postgresql+psycopg://avp:pw@localhost:5432/",  # DB 名無し: PGDATABASE で決まる
        "postgresql+psycopg://avp:pw@localhost:5432",
    ],
)
def test_a_url_that_does_not_fix_the_target_is_refused(url: str) -> None:
    with pytest.raises(UnsafeDatabaseError, match="明示|host|DB 名"):
        validate_test_database_url(url, environ=CLEAN_ENV)


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_libpq_environment_that_can_redirect_the_connection_is_refused(name: str) -> None:
    """URL が host / DB 名を明示していても、これらは接続時に接続先を変えうる。"""
    with pytest.raises(UnsafeDatabaseError, match=name):
        validate_test_database_url(TEST, environ={name: "10.0.0.5"})


def test_refusal_reads_the_real_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGSERVICE", "prod")
    with pytest.raises(UnsafeDatabaseError, match="PGSERVICE"):
        assert_destructive_allowed(TEST)


def test_pghost_does_not_matter_when_the_url_names_the_host() -> None:
    # URL の host は PGHOST より優先される。ただし host の無い URL は上で拒否される
    assert validate_test_database_url(TEST, environ={"PGHOST": "prod-db"}) == TEST


@pytest.mark.parametrize("host", ["postgres", "0.0.0.0", "db", "10.0.0.5"])
def test_only_loopback_names_are_local(host: str) -> None:
    """compose のサービス名 ``postgres`` は本番スタックの DB と同じ名前。接続先として許さない。"""
    url = f"postgresql+psycopg://avp:pw@{host}:5432/avp_test"
    with pytest.raises(UnsafeDatabaseError, match="ローカルではない"):
        validate_test_database_url(url, environ=CLEAN_ENV)
    assert validate_test_database_url(url, allow_remote=True, environ=CLEAN_ENV) == url


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"])
def test_loopback_test_database_is_accepted(host: str) -> None:
    url = f"postgresql+psycopg://avp:pw@{host}:5432/avp_test"
    assert validate_test_database_url(url, environ=CLEAN_ENV) == url


@pytest.mark.parametrize(
    "variant",
    [
        "postgresql+psycopg://x:y@localhost:5432/avp_test",
        "postgresql+psycopg://x:y@LOCALHOST:5432/avp_test",
        "postgresql+psycopg://x:y@127.0.0.1/avp_test",
        "postgresql+psycopg://x:y@postgres/avp_test?options=-c%20statement_timeout%3D1",
        "postgresql://x:y@localhost./avp_test",
        "postgresql+psycopg://x:y@0.0.0.0/avp_test",
    ],
)
def test_same_database_is_detected_through_url_variations(variant: str) -> None:
    """本番 URL が compose の ``postgres`` でも、``localhost`` の同じ DB は同一とみなす。"""
    with pytest.raises(UnsafeDatabaseError, match="DATABASE_URL"):
        validate_test_database_url(TEST, forbidden_urls=[variant], environ=CLEAN_ENV)


def test_different_port_or_database_is_not_the_same_database() -> None:
    other_port = "postgresql+psycopg://x:y@localhost:5433/avp_test"
    other_db = "postgresql+psycopg://x:y@localhost:5432/avp"
    assert validate_test_database_url(TEST, forbidden_urls=[other_port, other_db]) == TEST


@pytest.mark.parametrize("key", ["host", "hostaddr", "dbname", "port", "service"])
def test_libpq_override_in_query_is_refused(key: str) -> None:
    url = f"postgresql+psycopg://avp:pw@localhost:5432/avp_test?{key}=other"
    with pytest.raises(UnsafeDatabaseError, match="query"):
        validate_test_database_url(url, environ=CLEAN_ENV)


def test_explicit_allowlist_admits_a_non_test_name_but_never_the_app_database() -> None:
    scratch = "postgresql+psycopg://avp:pw@localhost:5432/avp_scratch"
    with pytest.raises(UnsafeDatabaseError):
        assert_destructive_allowed(scratch)
    assert_destructive_allowed(scratch, allowlist=frozenset({"avp_scratch"}))
    with pytest.raises(UnsafeDatabaseError):
        assert_destructive_allowed(PROD, allowlist=frozenset({"avp"}))


def test_empty_or_missing_url_is_refused() -> None:
    with pytest.raises(UnsafeDatabaseError):
        assert_destructive_allowed(None)
    with pytest.raises(UnsafeDatabaseError):
        assert_destructive_allowed("")


# ------------------------------------------------------------------ 動的検査（実接続）


def test_live_check_accepts_only_a_marked_test_database() -> None:
    check_live_identity("avp_test", TEST_DB_MARKER, expected_database="avp_test")
    # 引用符付きで作られた大文字名は別の DB。接尾辞は大文字小文字を区別して見る
    with pytest.raises(UnsafeDatabaseError, match="_test"):
        check_live_identity("AVP_TEST", TEST_DB_MARKER, expected_database="avp_test")


@pytest.mark.parametrize(
    "name,marker,expected,why",
    [
        ("avp", TEST_DB_MARKER, None, "_test"),  # 本番名。印があっても拒否
        ("avp", None, "avp_test", "_test"),
        ("temporal", TEST_DB_MARKER, None, "_test"),
        (
            "avp_test",
            None,
            "avp_test",
            "印",
        ),  # 印の無い _test DB（本番インスタンス上の avp_test を含む）
        ("avp_test", "something-else", "avp_test", "印"),
        ("other_test", TEST_DB_MARKER, "avp_test", "URL"),  # URL と別の _test DB
        (None, TEST_DB_MARKER, None, "_test"),
        ("", TEST_DB_MARKER, None, "_test"),
    ],
)
def test_live_check_refuses_everything_else(
    name: str | None, marker: str | None, expected: str | None, why: str
) -> None:
    with pytest.raises(UnsafeDatabaseError, match=why):
        check_live_identity(name, marker, expected_database=expected)


def test_a_connect_args_style_override_is_stopped_by_the_live_check() -> None:
    """URL は ``avp_test`` だが、実際には本番 ``avp`` に繋がっている（URL に現れない上書き）。

    静的検査は通る（URL は無害）。DDL が走る前に動的検査が止めること。
    """
    conn = _Conn(TEST, live=("avp", None))
    meta = _Metadata()
    assert_destructive_allowed(TEST)  # 静的検査はすり抜ける
    with pytest.raises(UnsafeDatabaseError, match="_test"):
        guarded_drop_all(conn, meta)
    with pytest.raises(UnsafeDatabaseError, match="_test"):
        guarded_create_all(conn, meta)
    assert meta.ddl == []


def test_a_marked_test_database_runs_the_ddl() -> None:
    conn = _Conn(TEST, live=("avp_test", TEST_DB_MARKER))
    meta = _Metadata()
    guarded_drop_all(conn, meta)
    guarded_create_all(conn, meta)
    assert meta.ddl == ["drop_all", "create_all"]


def test_an_unmarked_test_database_is_refused_even_with_a_clean_url() -> None:
    conn = _Conn(TEST, live=("avp_test", None))
    meta = _Metadata()
    with pytest.raises(UnsafeDatabaseError, match="印"):
        guarded_drop_all(conn, meta)
    assert meta.ddl == []


def test_drop_database_checks_the_target_and_the_connected_database() -> None:
    marked = _Conn(TEST, live=("avp_test", TEST_DB_MARKER))
    with pytest.raises(UnsafeDatabaseError, match="消す DB"):
        guarded_drop_database(marked, "avp")
    with pytest.raises(UnsafeDatabaseError, match="消す DB"):
        guarded_drop_database(marked, 'x"; DROP DATABASE avp; --_test')
    assert not any(s.startswith("DROP") for s in marked.statements)
    unmarked = _Conn(TEST, live=("avp_test", None))
    with pytest.raises(UnsafeDatabaseError, match="印"):
        guarded_drop_database(unmarked, "scratch_test")


# ------------------------------------------------------------------ 文の入口


class _Verify:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if not self.ok:
            raise UnsafeDatabaseError("live check failed")


@pytest.mark.parametrize(
    "statement",
    [
        'DROP TABLE "episodes"',
        "drop table if exists jobs cascade",
        'DROP SCHEMA "s1" CASCADE',
        "TRUNCATE episodes",
        "  \n DROP TABLE x",
        "/* harmless */ DROP TABLE x",
        "-- note\nDROP TABLE x",
        "SELECT 1; DROP TABLE x",
        "DROP DATABASE IF EXISTS scratch_test",
        # コメント・引用を正しく読む（独立レビュー E の P2-4）
        "/* ; */ DROP TABLE episodes",
        "/* a;b */\nDROP SCHEMA public CASCADE",
        "/* outer /* nested ; */ still comment ; */ DROP TABLE x",
        "-- ;\nDROP TABLE x",
        "SELECT 'it''s ; fine'; DROP TABLE x",
        "SELECT $$ ; $$; DROP TABLE x",
        "SELECT $t$ $$ ; $t$; DROP TABLE x",
        "DO $$ BEGIN EXECUTE 'DROP TABLE episodes'; END $$",
        "do $q$ begin execute format('%s', 'x'); end $q$",
        "DO $$ BEGIN DROP TABLE x; END $$",
        "ALTER TABLE t DROP COLUMN c",
        "alter table t drop c",
        "ALTER TABLE IF EXISTS t DROP COLUMN IF EXISTS c",
        "DELETE FROM t",
        "DELETE FROM t USING u",
        "SELECT 1; DELETE FROM t",
        "DELETE FROM t /* WHERE id = 1 */",
        "DELETE FROM t -- WHERE id = 1",
        "DELETE FROM t RETURNING 'where'",
    ],
)
def test_destructive_statements_are_verified_before_they_run(statement: str) -> None:
    verify = _Verify(ok=False)
    with pytest.raises(UnsafeDatabaseError, match="live check failed"):
        check_statement(statement, verify_connection=verify)
    assert verify.calls == 1


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "INSERT INTO t VALUES (1)",
        "DELETE FROM t WHERE id = 1",
        "ALTER TABLE t ADD COLUMN x int",
        "SELECT 'DROP TABLE x'",
        "CREATE TABLE t (id int)",
        # データを消さない ALTER は対象外（0017 のような CHECK 作り直しを止めない）
        "ALTER TABLE t DROP CONSTRAINT ck_t",
        "ALTER TABLE t ALTER COLUMN c DROP DEFAULT",
        "ALTER TABLE t ALTER COLUMN c DROP NOT NULL",
        "SELECT 'a;b' /* DROP TABLE x */ -- DROP TABLE y",
        "SELECT '; DROP TABLE x'",
        "DELETE FROM t WHERE id IN (SELECT id FROM u)",
        "DO $$ BEGIN PERFORM 1; END $$",
        "SELECT $$DROP TABLE x$$",
    ],
)
def test_other_statements_are_not_touched(statement: str) -> None:
    verify = _Verify(ok=False)
    check_statement(statement, verify_connection=verify)
    assert verify.calls == 0


def test_destructive_detector_scope_is_documented_in_one_place() -> None:
    """``is_destructive_statement`` は既定で WHERE 無し DELETE を数えない（architecture テストが
    リテラル SQL を拾うため）。実行時の守衛 ``check_statement`` だけが数える。"""
    assert not safety.is_destructive_statement("DELETE FROM t")
    assert safety.is_destructive_statement("DELETE FROM t", bulk_delete=True)
    assert safety.is_destructive_statement("/* ; */ DROP TABLE episodes")
    assert safety.is_destructive_statement("DO $$ BEGIN EXECUTE 'DROP TABLE x'; END $$")


def test_drop_database_names_must_end_with_test_even_on_a_verified_connection() -> None:
    check_statement("DROP DATABASE IF EXISTS scratch_test", verify_connection=_Verify(ok=True))
    check_statement('DROP DATABASE "scratch_test"', verify_connection=_Verify(ok=True))
    for victim in ("avp", '"avp"', "temporal"):
        with pytest.raises(UnsafeDatabaseError, match="DROP DATABASE"):
            check_statement(f"DROP DATABASE {victim}", verify_connection=_Verify(ok=True))


def test_require_test_database_url_refuses_prod_and_unset_means_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", PROD)
    with pytest.raises(UnsafeDatabaseError):
        safety.require_test_database_url()
    monkeypatch.delenv("TEST_DATABASE_URL")
    assert safety.require_test_database_url() is None
