"""破壊的スキーマ操作の単一ガード（INV-69 / ADR-0055）の検査。

本番DB（avp）・Temporal のDBを指す URL を、**接続を作る前に**拒否することを確かめる。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import MetaData
from sqlalchemy.engine import make_url

from infrastructure.db import safety
from infrastructure.db.safety import (
    UnsafeDatabaseError,
    assert_destructive_allowed,
    guarded_create_all,
    guarded_drop_all,
    validate_test_database_url,
)

PROD = "postgresql+psycopg://avp:pw@localhost:5432/avp"
TEST = "postgresql+psycopg://avp:pw@localhost:5432/avp_test"


class _NoConnect:
    """connect / execute / run_sync が呼ばれたら失敗する偽の接続（ガードは接続より前に効く）。"""

    def __init__(self, url: str) -> None:
        self.engine = SimpleNamespace(url=make_url(url))
        self.called: list[str] = []

    def _boom(self, name: str) -> Any:
        self.called.append(name)
        raise AssertionError(f"guard let {name}() run against a protected database")

    def execute(self, *a: Any, **k: Any) -> Any:
        return self._boom("execute")

    def run_sync(self, *a: Any, **k: Any) -> Any:
        return self._boom("run_sync")


@pytest.fixture(autouse=True)
def _app_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", PROD)
    monkeypatch.delenv("AVP_ALLOW_REMOTE_TEST_DB", raising=False)


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
        "postgresql://avp:pw@postgres/avp",
        "postgresql+psycopg://avp:pw@db.prod.example.com:5432/avp",
    ],
)
def test_protected_database_is_refused_before_any_connection(url: str) -> None:
    conn = _NoConnect(url)
    with pytest.raises(UnsafeDatabaseError):
        guarded_drop_all(conn, MetaData())  # type: ignore[arg-type]
    with pytest.raises(UnsafeDatabaseError):
        guarded_create_all(conn, MetaData())  # type: ignore[arg-type]
    assert conn.called == []


def test_engine_is_never_created_when_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlalchemy

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("create_engine called before the guard refused")

    monkeypatch.setattr(sqlalchemy, "create_engine", boom)
    monkeypatch.setenv("TEST_DATABASE_URL", PROD)
    with pytest.raises(UnsafeDatabaseError):
        safety.require_test_database_url()


@pytest.mark.parametrize(
    "variant",
    [
        "postgresql+psycopg://x:y@localhost:5432/avp_test",
        "postgresql+psycopg://x:y@LOCALHOST:5432/avp_test",
        "postgresql+psycopg://x:y@127.0.0.1/avp_test",
        "postgresql+psycopg://x:y@postgres/avp_test?options=-c%20statement_timeout%3D1",
        "postgresql://x:y@localhost./avp_test",
    ],
)
def test_same_database_is_detected_through_url_variations(variant: str) -> None:
    with pytest.raises(UnsafeDatabaseError, match="DATABASE_URL"):
        validate_test_database_url(TEST, forbidden_urls=[variant])


def test_different_port_or_database_is_not_the_same_database() -> None:
    other_port = "postgresql+psycopg://x:y@localhost:5433/avp_test"
    other_db = "postgresql+psycopg://x:y@localhost:5432/avp"
    assert validate_test_database_url(TEST, forbidden_urls=[other_port, other_db]) == TEST


def test_case_difference_in_database_name_counts_as_the_same_identity() -> None:
    with pytest.raises(UnsafeDatabaseError, match="DATABASE_URL"):
        validate_test_database_url(TEST, forbidden_urls=[TEST.replace("avp_test", "AVP_TEST")])


@pytest.mark.parametrize("key", ["host", "hostaddr", "dbname", "port", "service"])
def test_libpq_override_in_query_is_refused(key: str) -> None:
    # `?host=` / `?dbname=` は URL の host / database を上書きできる。名前検査をすり抜けるので拒否
    url = f"postgresql+psycopg://avp:pw@localhost:5432/avp_test?{key}=other"
    with pytest.raises(UnsafeDatabaseError, match="query"):
        validate_test_database_url(url, forbidden_urls=())


def test_test_suffix_database_is_allowed() -> None:
    calls: list[str] = []
    meta = SimpleNamespace(
        drop_all=lambda c: calls.append("drop"), create_all=lambda c: calls.append("create")
    )
    conn = _NoConnect(TEST)
    guarded_drop_all(conn, meta)  # type: ignore[arg-type]
    guarded_create_all(conn, meta)  # type: ignore[arg-type]
    assert calls == ["drop", "create"]


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


def test_tests_support_reexports_the_single_guard() -> None:
    from tests.support import db

    assert db.assert_destructive_allowed is safety.assert_destructive_allowed
    assert db.UnsafeTestDatabaseError is safety.UnsafeDatabaseError
