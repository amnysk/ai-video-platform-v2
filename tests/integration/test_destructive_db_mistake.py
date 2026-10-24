"""失敗注入 12: 破壊的な DB 操作が **誤って** 実行されても、DDL が走る前に拒否される（INV-83）。

実 PostgreSQL に「本番に見立てた DB」（名前が ``_test`` で終わらない・印なし・表と行あり）を作り、
URL・環境変数・``connect_args``・``creator`` の各経路で破壊的操作を試みる。どの経路でも
``UnsafeDatabaseError`` で止まり、**表と行が残っている** ことを実 DB で確認する。
最後に、印のあるテスト DB では同じ操作が通ること（ガードが何も止めない実装との区別）を確かめる。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from infrastructure.db.safety import UnsafeDatabaseError
from tests.support.db import (
    TEST_DB_MARKER,
    assert_destructive_allowed,
    guarded_drop_all,
    require_test_database_url,
)

TEST_DATABASE_URL = require_test_database_url()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL"
)

TAG = uuid.uuid4().hex[:8]
PRODLIKE = f"avp_g12_{TAG}"  # 本番に見立てる: _test で終わらず、印も無い
UNMARKED = f"avp_g12_{TAG}_unmarked_test"  # 名前は通るが印が無い
SAFE = f"avp_g12_{TAG}_safe_test"  # 印のあるテスト DB（対照）
METADATA = MetaData()
Table("episodes", METADATA, Column("id", Integer, primary_key=True))


def _url(database: str) -> Any:
    return make_url(TEST_DATABASE_URL or "").set(database=database)


def _dsn(database: str) -> str:
    return _url(database).render_as_string(hide_password=False).replace("+psycopg", "")


def _rows(database: str) -> int:
    """ガードを通さない読み取り専用の確認（生の psycopg）。"""
    with psycopg.connect(_dsn(database)) as conn:
        return conn.execute("SELECT count(*) FROM episodes").fetchone()[0]  # type: ignore[index]


def _engine(database: str, **kwargs: Any) -> Engine:
    return create_engine(_url(database), **kwargs)


@pytest.fixture(scope="module")
def world() -> Iterator[None]:
    """3 つの DB を作る。作ったのはこのテストなので、後始末は生の接続で行う。"""
    admin_url = _url(make_url(TEST_DATABASE_URL or "").database or "")
    assert_destructive_allowed(admin_url)
    names = (PRODLIKE, UNMARKED, SAFE)
    with psycopg.connect(_dsn(admin_url.database or ""), autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        admin.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(SAFE), sql.Literal(TEST_DB_MARKER)
            )
        )
    for name in names:
        with psycopg.connect(_dsn(name), autocommit=True) as conn:
            conn.execute("CREATE TABLE episodes (id integer primary key)")
            conn.execute("INSERT INTO episodes VALUES (1), (2), (3)")
    yield
    with psycopg.connect(_dsn(admin_url.database or ""), autocommit=True) as admin:
        assert_destructive_allowed(admin_url)  # 後始末の対象は、このテストが作った 3 つだけ
        for name in names:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _refused(action: Any) -> None:
    with pytest.raises(UnsafeDatabaseError):
        action()


def test_a_prod_like_url_is_refused_and_nothing_is_dropped(world: None) -> None:
    assert _rows(PRODLIKE) == 3
    _refused(lambda: assert_destructive_allowed(_url(PRODLIKE)))
    engine = _engine(PRODLIKE)
    try:
        with engine.connect() as conn:
            _refused(lambda: guarded_drop_all(conn, METADATA))
        with engine.connect() as conn:
            _refused(lambda: conn.execute(text("DROP TABLE episodes")))
        with engine.connect() as conn:
            _refused(lambda: conn.execute(text("TRUNCATE episodes")))
    finally:
        engine.dispose()
    assert _rows(PRODLIKE) == 3


def test_connect_args_override_to_the_prod_like_db_is_refused_by_the_live_check(
    world: None,
) -> None:
    """URL は印のあるテスト DB。``connect_args`` が本番に見立てた DB へ繋ぎ変える。"""
    assert_destructive_allowed(_url(SAFE))  # URL だけ見れば無害
    engine = _engine(SAFE, connect_args={"dbname": PRODLIKE})
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar_one() == PRODLIKE
            _refused(lambda: guarded_drop_all(conn, METADATA))
        with engine.connect() as conn:
            _refused(lambda: conn.execute(text("DROP TABLE episodes")))
        with engine.connect() as conn:
            _refused(
                lambda: METADATA.drop_all(conn)
            )  # ガードを呼ばない drop_all も文の入口で止まる
    finally:
        engine.dispose()
    assert _rows(PRODLIKE) == 3
    assert _rows(SAFE) == 3


def test_creator_override_to_the_prod_like_db_is_refused(world: None) -> None:
    assert_destructive_allowed(_url(SAFE))  # URL だけ見れば無害。実接続は別の DB
    engine = create_engine(
        _url(SAFE), creator=lambda: psycopg.connect(_dsn(PRODLIKE)), pool_pre_ping=False
    )
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar_one() == PRODLIKE
            _refused(lambda: guarded_drop_all(conn, METADATA))
        with engine.connect() as conn:
            _refused(lambda: conn.execute(text("DROP TABLE episodes")))
    finally:
        engine.dispose()
    assert _rows(PRODLIKE) == 3


def test_an_unmarked_test_named_db_is_refused(world: None) -> None:
    """名前が ``_test`` で終わるだけの DB（本番上の avp_test を含む）は印が無ければ拒否。"""
    assert_destructive_allowed(_url(UNMARKED))  # 静的検査は通る。止めるのは印
    engine = _engine(UNMARKED)
    try:
        with engine.connect() as conn:
            _refused(lambda: guarded_drop_all(conn, METADATA))
        with engine.connect() as conn:
            _refused(lambda: conn.execute(text("DROP TABLE episodes")))
    finally:
        engine.dispose()
    assert _rows(UNMARKED) == 3


def test_libpq_environment_cannot_redirect_a_destructive_operation(
    world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_less = URL.create("postgresql+psycopg", username="u", password="p", database=SAFE)
    monkeypatch.setenv("PGHOST", "localhost")
    _refused(lambda: assert_destructive_allowed(host_less))
    monkeypatch.delenv("PGHOST")
    for name in ("PGHOSTADDR", "PGSERVICE"):
        monkeypatch.setenv(name, "127.0.0.1" if name == "PGHOSTADDR" else "prod")
        _refused(lambda: assert_destructive_allowed(_url(SAFE)))
        monkeypatch.delenv(name)
    assert _rows(PRODLIKE) == 3


def test_dropping_the_prod_like_database_itself_is_refused(world: None) -> None:
    """印のあるテスト DB から ``DROP DATABASE <本番に見立てた名前>`` を流しても止まる。"""
    assert_destructive_allowed(_url(SAFE))
    engine = _engine(SAFE, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            _refused(lambda: conn.execute(text(f'DROP DATABASE "{PRODLIKE}"')))
    finally:
        engine.dispose()
    assert _rows(PRODLIKE) == 3


def test_control_a_marked_test_database_does_accept_the_same_operations(world: None) -> None:
    """ガードが何も通さない実装では、上の拒否テストが全部通ってしまう。対照として通ることを確かめる。"""
    engine = _engine(SAFE)
    try:
        with engine.begin() as conn:
            guarded_drop_all(conn, METADATA)
    finally:
        engine.dispose()
    with psycopg.connect(_dsn(SAFE)) as conn:
        exists = conn.execute("SELECT to_regclass('public.episodes')").fetchone()[0]  # type: ignore[index]
    assert exists is None, "印のあるテスト DB への drop_all が実行されていない"
