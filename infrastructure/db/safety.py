"""破壊的なスキーマ操作の単一ガード（INV-69 / ADR-0055）。

``drop_all`` / ``create_all`` / ``DROP SCHEMA`` / ``DROP DATABASE`` は、接続先DBの名前が
``_test`` で終わる（または呼び出し側が明示した allowlist にある）ときだけ許す。
判定は **URL だけ**で行い、接続を作る前に例外にする（検査のために本番へ繋がない）。

これを呼ばずに上記の操作を書いてはいけない（``tests/architecture/test_destructive_schema_ops_guarded.py``）。
tests/support/db.py はこのモジュールを再公開するだけで、規則を別に持たない（AGENTS §8）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "0.0.0.0"})
TEST_SUFFIX = "_test"
# libpq は URL の query で接続先を上書きできる。名前検査をすり抜けるので許さない
OVERRIDING_QUERY_KEYS: frozenset[str] = frozenset(
    {"host", "hostaddr", "dbname", "database", "port", "service", "servicefile"}
)


class UnsafeDatabaseError(RuntimeError):
    """破壊的操作の対象として安全と確認できないDB URL。"""


# 既存の名前（tests/support/db.py と各テストが使う）
UnsafeTestDatabaseError = UnsafeDatabaseError


def _parse(url: str) -> URL:
    try:
        return make_url(url)
    except ArgumentError as exc:
        raise UnsafeDatabaseError("DB URL を解釈できない") from exc


def _host(url: URL) -> str:
    return (url.host or "localhost").lower().rstrip(".")


def _identity(url: URL) -> tuple[str, int, str]:
    """ドライバ・認証情報・query の違いを無視した「同じDBか」の鍵。DB名は大文字小文字を畳む。"""
    host = _host(url)
    # localhost / 127.0.0.1 / ::1 / postgres は同じサーバの別表記になりうる。1つの鍵に畳む
    if host in LOCAL_HOSTS:
        host = "<local>"
    return (host, url.port or 5432, (url.database or "").lower())


def validate_test_database_url(
    url: str,
    *,
    forbidden_urls: Iterable[str | None] = (),
    allow_remote: bool = False,
    allowlist: frozenset[str] = frozenset(),
) -> str:
    """破壊的操作してよいURLならそのまま返し、そうでなければ ``UnsafeDatabaseError``。"""
    if not url:
        raise UnsafeDatabaseError("DB URL が空")
    parsed = _parse(url)
    if not parsed.drivername.startswith("postgresql"):
        raise UnsafeDatabaseError("破壊的操作の対象は PostgreSQL であること")
    overriding = sorted(k for k in parsed.query if k.lower() in OVERRIDING_QUERY_KEYS)
    if overriding:
        raise UnsafeDatabaseError(f"URL の query が接続先を上書きできる: {overriding}")
    database = parsed.database or ""
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
    """破壊的操作の直前に呼ぶ。接続は作らない。"""
    text = url.render_as_string(hide_password=False) if isinstance(url, URL) else (url or "")
    validate_test_database_url(
        text,
        forbidden_urls=_application_urls(),
        allow_remote=_allow_remote(),
        allowlist=allowlist,
    )


def guarded_drop_all(connection: Any, metadata: Any) -> None:
    """``metadata.drop_all(connection)``。URL を先に検査する（``conn.run_sync`` 経由）。"""
    assert_destructive_allowed(connection.engine.url)
    metadata.drop_all(connection)


def guarded_create_all(connection: Any, metadata: Any) -> None:
    """``metadata.create_all(connection)``。接続先 URL を先に検査する。"""
    assert_destructive_allowed(connection.engine.url)
    metadata.create_all(connection)
