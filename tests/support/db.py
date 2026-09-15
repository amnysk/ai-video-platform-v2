"""integration テストが使う PostgreSQL の URL と、破壊的操作の安全装置（ADR-0021）。

integration テストは ``drop_all`` / ``DROP SCHEMA`` / ``DROP DATABASE`` を実行する。
それが開発DB（``avp``）や本番DBへ向かうと、テストがデータを消す。そのため:

- 読むのは ``TEST_DATABASE_URL`` **だけ**（``DATABASE_URL`` へ黙って落ちない）
- DB名は ``_test`` で終わること
- ホストはローカル（localhost / 127.0.0.1 / ::1 / compose の ``postgres``）。
  それ以外は ``AVP_ALLOW_REMOTE_TEST_DB=1`` の明示が要る
- アプリの接続先（``DATABASE_URL`` / ``Settings().database_url``）と同じDBであってはならない

条件を満たさない URL は skip ではなく**例外**（収集時点で止める）。未設定だけが skip。
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
TEST_SUFFIX = "_test"


class UnsafeTestDatabaseError(RuntimeError):
    """テスト用として安全と確認できないDB URL。"""


def _parse(url: str) -> URL:
    try:
        return make_url(url)
    except ArgumentError as exc:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL を解釈できない") from exc


def _identity(url: URL) -> tuple[str, int, str]:
    """ドライバ名・認証情報の違いを無視した「同じDBか」の鍵。"""
    host = (url.host or "localhost").lower()
    # localhost / 127.0.0.1 / ::1 / postgres は同じサーバの別表記になりうる。1つの鍵に畳む
    if host in LOCAL_HOSTS:
        host = "<local>"
    return (host, url.port or 5432, url.database or "")


def validate_test_database_url(
    url: str,
    *,
    forbidden_urls: Iterable[str | None],
    allow_remote: bool = False,
) -> str:
    """安全なテスト用URLならそのまま返し、そうでなければ ``UnsafeTestDatabaseError``。"""
    if not url:
        raise UnsafeTestDatabaseError("テスト用DB URL が空")
    parsed = _parse(url)
    if not parsed.drivername.startswith("postgresql"):
        raise UnsafeTestDatabaseError("テスト用DB は PostgreSQL であること")
    database = parsed.database or ""
    if not database.endswith(TEST_SUFFIX):
        raise UnsafeTestDatabaseError(
            f"テスト用DB名は {TEST_SUFFIX!r} で終わること（got {database!r}）"
        )
    host = (parsed.host or "localhost").lower()
    if host not in LOCAL_HOSTS and not allow_remote:
        raise UnsafeTestDatabaseError(
            f"テスト用DB の host {host!r} はローカルではない（AVP_ALLOW_REMOTE_TEST_DB=1 で許可）"
        )
    for forbidden in forbidden_urls:
        if not forbidden:
            continue
        try:
            other = make_url(forbidden)
        except ArgumentError:
            continue
        if _identity(other) == _identity(parsed):
            raise UnsafeTestDatabaseError("テスト用DB がアプリの DATABASE_URL と同じDBを指している")
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


def assert_destructive_allowed(url: str | None) -> None:
    """``drop_all`` / ``create_all`` / ``DROP SCHEMA`` / ``DROP DATABASE`` の直前に呼ぶ。"""
    validate_test_database_url(
        url or "", forbidden_urls=_application_urls(), allow_remote=_allow_remote()
    )
