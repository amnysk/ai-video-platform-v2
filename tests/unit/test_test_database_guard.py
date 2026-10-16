"""破壊的なテスト（drop_all / DROP SCHEMA / DROP DATABASE）が開発・本番DBへ向かないことの検査。

integration テストは ``TEST_DATABASE_URL`` だけを読み、``_test`` で終わるローカルDBしか受け付けない
（ADR-0021）。
"""

from __future__ import annotations

import pytest

from tests.support.db import (
    UnsafeTestDatabaseError,
    assert_destructive_allowed,
    require_test_database_url,
    validate_test_database_url,
)

DEV = "postgresql+psycopg://avp:pw@localhost:5432/avp"
TEST = "postgresql+psycopg://avp:pw@localhost:5432/avp_test"


def test_dev_database_is_rejected() -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="_test"):
        validate_test_database_url(DEV, forbidden_urls=())


def test_remote_host_is_rejected_even_with_test_suffix() -> None:
    url = "postgresql+psycopg://avp:pw@db.prod.example.com:5432/avp_test"
    with pytest.raises(UnsafeTestDatabaseError, match="host"):
        validate_test_database_url(url, forbidden_urls=())


def test_remote_host_is_accepted_only_with_explicit_opt_in() -> None:
    url = "postgresql+psycopg://avp:pw@ci-db:5432/avp_test"
    assert validate_test_database_url(url, forbidden_urls=(), allow_remote=True) == url


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "postgres"])
def test_local_test_database_is_accepted(host: str) -> None:
    url = (
        f"postgresql+psycopg://avp:pw@[{host}]:5432/avp_test"
        if ":" in host
        else (f"postgresql+psycopg://avp:pw@{host}:5432/avp_test")
    )
    assert validate_test_database_url(url, forbidden_urls=()) == url


def test_url_equal_to_application_database_is_rejected() -> None:
    other_driver = "postgresql://avp:other@localhost/avp_test"
    with pytest.raises(UnsafeTestDatabaseError, match="DATABASE_URL"):
        validate_test_database_url(TEST, forbidden_urls=(None, other_driver))


def test_non_postgres_url_is_rejected() -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        validate_test_database_url("sqlite:///x_test", forbidden_urls=())


def test_unset_test_database_url_means_skip(monkeypatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST)  # DATABASE_URL は決して読まない
    assert require_test_database_url() is None


def test_require_refuses_dev_database_as_hard_error(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", DEV)
    with pytest.raises(UnsafeTestDatabaseError):
        require_test_database_url()


def test_require_refuses_when_equal_to_database_url_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", TEST)
    monkeypatch.setenv("DATABASE_URL", TEST)
    with pytest.raises(UnsafeTestDatabaseError, match="DATABASE_URL"):
        require_test_database_url()


def test_require_accepts_isolated_test_database(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", TEST)
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.delenv("AVP_ALLOW_REMOTE_TEST_DB", raising=False)
    assert require_test_database_url() == TEST


def test_destructive_operation_refuses_non_test_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    with pytest.raises(UnsafeTestDatabaseError):
        assert_destructive_allowed(DEV)
    with pytest.raises(UnsafeTestDatabaseError):
        assert_destructive_allowed("")
    assert_destructive_allowed(TEST)


@pytest.mark.parametrize("app_host", ["localhost", "127.0.0.1", "[::1]", "postgres"])
def test_local_host_aliases_of_the_application_db_are_the_same_db(app_host: str) -> None:
    """localhost と 127.0.0.1 などの表記違いで「アプリと同じDB」の検査をすり抜けない。"""
    app = f"postgresql+psycopg://avp:x@{app_host}:5432/avp_test"
    with pytest.raises(UnsafeTestDatabaseError):
        validate_test_database_url(
            "postgresql+psycopg://avp:y@127.0.0.1:5432/avp_test", forbidden_urls=[app]
        )
