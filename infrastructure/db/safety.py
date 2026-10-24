"""破壊的な DB 操作の単一ガード（INV-69 / INV-83 / ADR-0055）。

``drop_all`` / ``create_all`` / ``DROP TABLE|SCHEMA|DATABASE`` / ``TRUNCATE`` は、次の
**両方** を満たすときだけ許す。

1. **静的**（接続を作る前）: URL が接続先を完全に決めていて（host と DB 名が明示、libpq の
   環境変数・service ファイルに左右されない）、DB 名が ``_test`` で終わり（または allowlist）、
   ローカル host で、アプリの ``DATABASE_URL`` と同じ DB ではない。
2. **動的**（実行直前、**実際に繋がっている接続**に対して）: ``current_database()`` が
   ``_test`` で終わり（または allowlist）、URL の DB 名と一致し、DB に **テスト用の印**
   （DB コメント ``avp-test-database``）がある。``connect_args`` / ``creator`` / ``PGHOST``
   等で URL と違う所へ繋がっていても、ここで止まる。

静的検査だけでは、URL に現れない接続先の上書きを見られない。動的検査は DB 名と印だけを見る:
本番と ``avp_test`` が同じ PostgreSQL インスタンスにあるとき、サーバのアドレスやポートでは
区別できないので、DB 単位の事実（名前・印）で区別する。**別インスタンスへの分離が最終形**
（docs/operations/db-role-separation.md）。

これを呼ばずに破壊的操作を書いてはいけない
（``tests/architecture/test_destructive_schema_ops_guarded.py``）。
tests/support/db.py はこのモジュールを再公開するだけで、規則を別に持たない（AGENTS §8）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError

#: 破壊的操作の接続先として許す host。**compose のサービス名（``postgres``）は入れない**:
#: 同じ名前が本番スタックの PostgreSQL を指しうる。``0.0.0.0`` も入れない（待受アドレス）。
#: それ以外は ``AVP_ALLOW_REMOTE_TEST_DB=1`` の明示が要る
LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
#: 「同じサーバ」とみなす別名の集合。**許可ではなく同一性の判定だけ**に使う
#: （本番 URL が ``postgres`` でも ``localhost`` の ``avp_test`` と同じインスタンスになりうる）
_SAME_SERVER_ALIASES: frozenset[str] = LOCAL_HOSTS | {"postgres", "0.0.0.0"}
TEST_SUFFIX = "_test"
#: DB コメントに置く印。``COMMENT ON DATABASE <db> IS 'avp-test-database'``
TEST_DB_MARKER = "avp-test-database"
# libpq は URL の query で接続先を上書きできる。名前検査をすり抜けるので許さない
OVERRIDING_QUERY_KEYS: frozenset[str] = frozenset(
    {"host", "hostaddr", "dbname", "database", "port", "service", "servicefile"}
)
#: 設定されていると URL に現れない所へ繋がりうる libpq の環境変数（URL が host・DB を明示していても
#: ``PGHOSTADDR`` / ``PGSERVICE`` は接続先を変えうるので常に拒否する）
_ALWAYS_REFUSED_ENV: tuple[str, ...] = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
_DESTRUCTIVE_STATEMENT = re.compile(
    r"^\s*(?:DROP\s+(?P<kind>TABLE|SCHEMA|DATABASE)\b(?:\s+IF\s+EXISTS)?(?:\s+(?P<name>\"[^\"]+\"|\w+))?"
    r"|(?P<truncate>TRUNCATE)\b)",
    re.IGNORECASE,
)
_DO_BLOCK = re.compile(r"^\s*DO\b", re.IGNORECASE)
_EXECUTE_WORD = re.compile(r"\bEXECUTE\b", re.IGNORECASE)
_ANY_DESTRUCTIVE_WORD = re.compile(r"\b(?:DROP|TRUNCATE)\b", re.IGNORECASE)
_ALTER_TABLE = re.compile(r"^\s*ALTER\s+TABLE\b", re.IGNORECASE)
#: ``ALTER TABLE .. DROP [COLUMN] c`` は列とそのデータを消す（COLUMN は省略できる）。
#: ``DROP CONSTRAINT`` と ``ALTER COLUMN .. DROP DEFAULT|NOT NULL|IDENTITY|EXPRESSION`` は
#: データを消さない
_DROP_COLUMN = re.compile(
    r"\bDROP\s+(?!(?:CONSTRAINT|DEFAULT|NOT|IDENTITY|EXPRESSION)\b)", re.IGNORECASE
)
_BULK_DELETE = re.compile(r"^\s*DELETE\s+FROM\b", re.IGNORECASE)
_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")


class UnsafeDatabaseError(RuntimeError):
    """破壊的操作の対象として安全と確認できないDB。"""


# 既存の名前（tests/support/db.py と各テストが使う）
UnsafeTestDatabaseError = UnsafeDatabaseError


def _parse(url: str) -> URL:
    try:
        return make_url(url)
    except ArgumentError as exc:
        raise UnsafeDatabaseError("DB URL を解釈できない") from exc


def _host(url: URL) -> str:
    return (url.host or "").lower().rstrip(".")


def _identity(url: URL) -> tuple[str, int, str]:
    """ドライバ・認証情報・query の違いを無視した「同じDBか」の鍵。DB名は大文字小文字を畳む。"""
    host = _host(url) or "localhost"
    # localhost / 127.0.0.1 / ::1 / postgres は同じサーバの別表記になりうる。1つの鍵に畳む
    if host in _SAME_SERVER_ALIASES:
        host = "<local>"
    return (host, url.port or 5432, (url.database or "").lower())


def validate_test_database_url(
    url: str,
    *,
    forbidden_urls: Iterable[str | None] = (),
    allow_remote: bool = False,
    allowlist: frozenset[str] = frozenset(),
    environ: Mapping[str, str] | None = None,
) -> str:
    """破壊的操作してよいURLならそのまま返し、そうでなければ ``UnsafeDatabaseError``。

    URL が接続先（host / DB 名）を **明示** していることを要求する。host の無い URL は libpq が
    ``PGHOST`` / ``PGSERVICE`` / ``PGHOSTADDR`` を接続時に読んで決めるので、
    「localhost のはず」とは言えない。
    """
    env = os.environ if environ is None else environ
    if not url:
        raise UnsafeDatabaseError("DB URL が空")
    parsed = _parse(url)
    if not parsed.drivername.startswith("postgresql"):
        raise UnsafeDatabaseError("破壊的操作の対象は PostgreSQL であること")
    overriding = sorted(k for k in parsed.query if k.lower() in OVERRIDING_QUERY_KEYS)
    if overriding:
        raise UnsafeDatabaseError(f"URL の query が接続先を上書きできる: {overriding}")
    set_env = sorted(k for k in _ALWAYS_REFUSED_ENV if env.get(k))
    if set_env:
        raise UnsafeDatabaseError(
            f"libpq の環境変数が接続先を上書きしうる: {set_env}（unset してから実行する）"
        )
    if not parsed.host:
        raise UnsafeDatabaseError(
            "URL に host が無い。libpq が PGHOST 等から接続時に決めるので、host を明示すること"
        )
    if not parsed.database:
        raise UnsafeDatabaseError("URL に DB 名が無い（PGDATABASE に左右される）。明示すること")
    database = parsed.database
    if not (database.endswith(TEST_SUFFIX) or database in allowlist):
        raise UnsafeDatabaseError(f"DB名は {TEST_SUFFIX!r} で終わること（got {database!r}）")
    host = _host(parsed)
    if host not in LOCAL_HOSTS and not allow_remote:
        raise UnsafeDatabaseError(
            f"host {host!r} はローカルではない（AVP_ALLOW_REMOTE_TEST_DB=1 で許可）"
        )
    for forbidden in forbidden_urls:
        if not forbidden:
            continue
        try:
            other = make_url(forbidden)
        except ArgumentError:
            continue
        if _identity(other) == _identity(parsed):
            raise UnsafeDatabaseError("対象DB がアプリの DATABASE_URL と同じDBを指している")
    return url


def _application_urls() -> list[str | None]:
    from infrastructure.config import Settings

    return [os.environ.get("DATABASE_URL"), Settings().database_url]


def _allow_remote() -> bool:
    return os.environ.get("AVP_ALLOW_REMOTE_TEST_DB") == "1"


def require_test_database_url() -> str | None:
    """``TEST_DATABASE_URL`` を検証して返す。未設定なら ``None``（呼び出し側で skip）。"""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return None
    return validate_test_database_url(
        url, forbidden_urls=_application_urls(), allow_remote=_allow_remote()
    )


def assert_destructive_allowed(
    url: str | URL | None, *, allowlist: frozenset[str] = frozenset()
) -> None:
    """破壊的操作の直前に呼ぶ（**静的**検査のみ。接続は作らない）。

    実際に繋がっている接続の検査は ``assert_live_test_database`` / ``guarded_*`` が行う。
    """
    rendered = url.render_as_string(hide_password=False) if isinstance(url, URL) else (url or "")
    validate_test_database_url(
        rendered,
        forbidden_urls=_application_urls(),
        allow_remote=_allow_remote(),
        allowlist=allowlist,
    )


# ------------------------------------------------------------------ 動的検査（実接続）

_LIVE_SQL = (
    "SELECT current_database(), "
    "(SELECT shobj_description(d.oid, 'pg_database') FROM pg_database d "
    "WHERE d.datname = current_database())"
)


def check_live_identity(
    current_database: str | None,
    marker: str | None,
    *,
    expected_database: str | None = None,
    allowlist: frozenset[str] = frozenset(),
) -> None:
    """接続先の実際の DB 名と印を判定する（純粋関数。接続は持たない）。"""
    name = current_database or ""
    if not (name.endswith(TEST_SUFFIX) or name in allowlist):
        raise UnsafeDatabaseError(
            f"実際の接続先 DB {name!r} は {TEST_SUFFIX!r} で終わらない"
            "（URL と違う所へ繋がっている）"
        )
    if expected_database is not None and name.lower() != expected_database.lower():
        raise UnsafeDatabaseError(
            f"実際の接続先 DB {name!r} が URL の DB {expected_database!r} と違う"
        )
    if marker != TEST_DB_MARKER:
        raise UnsafeDatabaseError(
            f"DB {name!r} にテスト用の印が無い。確認した上で "
            f"COMMENT ON DATABASE \"{name}\" IS '{TEST_DB_MARKER}' を実行する"
        )


def assert_live_test_database(
    connection: Any,
    *,
    expected_database: str | None = None,
    allowlist: frozenset[str] = frozenset(),
) -> None:
    """**実際に繋がっている** SQLAlchemy の同期 ``Connection`` が、印のあるテスト DB かを確かめる。

    SELECT だけを実行する。URL を見ない
    （``connect_args`` / ``creator`` / 環境変数の上書きを検出するため）。
    """
    row = connection.execute(text(_LIVE_SQL)).one()
    check_live_identity(row[0], row[1], expected_database=expected_database, allowlist=allowlist)


def mark_test_database(url: str | URL) -> None:
    """``TEST_DATABASE_URL`` の DB に印を付ける（``AVP_MARK_TEST_DB=1`` のときだけ呼ぶ）。

    静的検査を通った ``_test`` DB にしか付けない。``COMMENT`` は破壊的ではない。
    """
    from sqlalchemy import create_engine

    rendered = url.render_as_string(hide_password=False) if isinstance(url, URL) else url
    validate_test_database_url(
        rendered, forbidden_urls=_application_urls(), allow_remote=_allow_remote()
    )
    parsed = make_url(rendered)
    engine = create_engine(rendered, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            name = conn.execute(text("SELECT current_database()")).scalar_one()
            if name.lower() != (parsed.database or "").lower() or not name.endswith(TEST_SUFFIX):
                raise UnsafeDatabaseError(f"実際の接続先 {name!r} が URL と違う。印を付けない")
            conn.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{TEST_DB_MARKER}'"))
    finally:
        engine.dispose()


def guarded_drop_all(connection: Any, metadata: Any) -> None:
    """``metadata.drop_all(connection)``。URL（静的）と実接続（動的）の両方を検査してから実行する。"""
    assert_destructive_allowed(connection.engine.url)
    assert_live_test_database(connection, expected_database=connection.engine.url.database)
    metadata.drop_all(connection)


def guarded_create_all(connection: Any, metadata: Any) -> None:
    """``metadata.create_all(connection)``。URL（静的）と実接続（動的）の両方を検査してから実行する。"""
    assert_destructive_allowed(connection.engine.url)
    assert_live_test_database(connection, expected_database=connection.engine.url.database)
    metadata.create_all(connection)


def guarded_drop_database(
    connection: Any, name: str, *, allowlist: frozenset[str] = frozenset()
) -> None:
    """``DROP DATABASE``。**消す対象の名前**と、いま繋がっている DB の両方を検査する。

    対象は ``_test`` で終わる（または allowlist）こと。
    繋がっている DB も印のあるテスト DB であること。
    ``connection`` は AUTOCOMMIT の同期 ``Connection``。
    """
    if not (name.endswith(TEST_SUFFIX) or name in allowlist) or not re.fullmatch(r"\w+", name):
        raise UnsafeDatabaseError(f"消す DB {name!r} は {TEST_SUFFIX!r} で終わらない")
    assert_destructive_allowed(connection.engine.url, allowlist=allowlist)
    assert_live_test_database(connection, allowlist=allowlist)
    connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


# ------------------------------------------------------------------ 文の入口での検査

_VERIFIED_KEY = "avp_live_verified"


@dataclass(frozen=True)
class _Destructive:
    #: TABLE / SCHEMA / DATABASE / TRUNCATE / DROP COLUMN / DYNAMIC / BULK DELETE
    kind: str
    name: str = ""


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _split_statements(sql: str) -> list[tuple[str, str]]:
    """SQL を文に分ける。``(raw, skeleton)`` の組を返す。

    コメント（``--``・入れ子の ``/* */``）は取り除き、``;`` の分割は **引用の外だけ**
    （``'..'`` ``E'..'`` ``".."`` ``$tag$..$tag$``）。分割前にコメントを剥がすと
    ``/* ; */ DROP ..`` が見えなくなり、分割後に剥がすと ``'; '`` の中の ``;`` で誤分割する。
    ``raw`` は引用の中身を残し（動的 SQL の検出用）、``skeleton`` は中身を空白にする
    （``WHERE`` 等の語を文字列の中から拾わないため）。
    閉じていない引用・コメントは末尾まで続くものとする。
    """
    out: list[tuple[str, str]] = []
    raw: list[str] = []
    skel: list[str] = []
    n = len(sql)
    i = 0

    def flush() -> None:
        r = "".join(raw)
        if r.strip():
            out.append((r, "".join(skel)))
        raw.clear()
        skel.clear()

    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            raw.append(" ")
            skel.append(" ")
        elif two == "/*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            raw.append(" ")
            skel.append(" ")
        elif ch in ("'", '"'):
            escapes = (
                ch == "'"
                and i > 0
                and sql[i - 1] in "eE"
                and (i < 2 or not _is_word_char(sql[i - 2]))
            )
            j = i + 1
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                elif sql[j] == ch:
                    if sql.startswith(ch, j + 1):
                        j += 2
                    else:
                        break
                else:
                    j += 1
            end = min(j + 1, n)
            raw.append(sql[i:end])
            skel.append(" '' " if ch == "'" else sql[i:end])
            i = end
        elif ch == "$" and (i == 0 or not _is_word_char(sql[i - 1])) and _DOLLAR_TAG.match(sql, i):
            tag = _DOLLAR_TAG.match(sql, i)
            assert tag is not None
            close = sql.find(tag.group(), tag.end())
            end = n if close < 0 else close + len(tag.group())
            raw.append(sql[i:end])
            skel.append(" '' ")
            i = end
        elif ch == ";":
            flush()
            i += 1
        else:
            raw.append(ch)
            skel.append(ch)
            i += 1
    flush()
    return out


def _destructive_matches(statement: str, *, bulk_delete: bool = False) -> list[_Destructive]:
    """文ごとに破壊的かを判定する。迷うときは破壊的（安全側）とする。

    - ``DROP TABLE|SCHEMA|DATABASE`` / ``TRUNCATE``（文頭。コメント・引用を正しく読んだ上で）
    - ``ALTER TABLE .. DROP [COLUMN] c``（列のデータが消える。``DROP CONSTRAINT`` は含めない:
      データを消さず、migration が日常的に行う）
    - ``DO`` ブロックで ``EXECUTE`` を含むもの、または ``DROP`` / ``TRUNCATE`` の語を含むもの
      （動的 SQL は中身を静的に確定できない）
    - ``bulk_delete=True`` のときだけ ``WHERE`` の無い ``DELETE FROM``（テスト DB 守衛用）
    """
    found: list[_Destructive] = []
    for raw, skel in _split_statements(statement):
        m = _DESTRUCTIVE_STATEMENT.match(raw)
        if m is not None:
            kind = (m.group("kind") or "TRUNCATE").upper()
            found.append(_Destructive(kind, (m.group("name") or "").strip('"')))
        elif _DO_BLOCK.match(raw) and (
            _EXECUTE_WORD.search(raw) or _ANY_DESTRUCTIVE_WORD.search(raw)
        ):
            found.append(_Destructive("DYNAMIC"))
        elif _ALTER_TABLE.match(skel) and _DROP_COLUMN.search(skel):
            found.append(_Destructive("DROP COLUMN"))
        elif bulk_delete and _BULK_DELETE.match(skel) and not _WHERE.search(skel):
            found.append(_Destructive("BULK DELETE"))
    return found


def is_destructive_statement(statement: str, *, bulk_delete: bool = False) -> bool:
    """SQL 文字列に破壊的な文（``_destructive_matches`` の規則）があれば True。"""
    return bool(_destructive_matches(statement, bulk_delete=bulk_delete))


def check_statement(
    statement: str,
    *,
    verify_connection: Callable[[], None],
    allowlist: frozenset[str] = frozenset(),
) -> None:
    """破壊的な文なら、実行前に接続先を検証する。

    規則は ``_destructive_matches``（``WHERE`` 無しの ``DELETE`` を含む）。

    ``DROP DATABASE`` は消す対象の名前も ``_test`` で終わることを要求する。
    それ以外の文は何もしない。
    """
    for match in _destructive_matches(statement, bulk_delete=True):
        verify_connection()
        if match.kind == "DATABASE":
            target = match.name
            if not (target.endswith(TEST_SUFFIX) or target in allowlist):
                raise UnsafeDatabaseError(
                    f"DROP DATABASE の対象 {target!r} は {TEST_SUFFIX!r} で終わらない"
                )


def install_destructive_statement_guard(
    engine_class: type[Engine] = Engine, *, allowlist: frozenset[str] = frozenset()
) -> Callable[[], None]:
    """全ての PostgreSQL 接続で、破壊的な文の **実行直前** に実接続を検証する。

    pytest の conftest から呼ぶ。

    ``drop_all`` も生 SQL も ``connect_args`` を上書きした engine も、ここを通る。SQLite は対象外
    （本番に繋がらない）。検証結果は接続ごとに ``Connection.info`` に残す。返り値は解除関数。
    """

    def before_cursor_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if conn.dialect.name != "postgresql":
            return

        def verify() -> None:
            if conn.info.get(_VERIFIED_KEY):
                return
            cur = conn.connection.cursor()
            try:
                cur.execute(_LIVE_SQL)
                row = cur.fetchone()
            finally:
                cur.close()
            check_live_identity(row[0], row[1], allowlist=allowlist)
            conn.info[_VERIFIED_KEY] = True

        check_statement(statement, verify_connection=verify, allowlist=allowlist)

    event.listen(engine_class, "before_cursor_execute", before_cursor_execute)

    def remove() -> None:
        event.remove(engine_class, "before_cursor_execute", before_cursor_execute)

    return remove
