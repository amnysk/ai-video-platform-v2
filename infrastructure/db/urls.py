"""接続URLの解決。**ここが唯一の定義**（AGENTS.md §8）。

psycopg v3 は `postgresql+psycopg://` ひとつで sync / async の両方を提供する
（ADR-0008）。したがって PostgreSQL では変換が不要になり、以前の
`url.replace("+asyncpg", "")` のような**ブラックリスト式の文字列置換を持たない**。
置換方式は「新しいドライバを足した人が1行を更新し忘れると、差分に現れない側が壊れる」
という、このプロジェクトが繰り返し踏んできた事故の形だった。
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

#: 同期ドライバ名 <- 非同期ドライバ名。ホワイトリスト。
#: 新しい非同期ドライバを足すときは**ここだけ**を更新する。
SYNC_DRIVER_BY_ASYNC_DRIVER: dict[str, str] = {
    "aiosqlite": "pysqlite",
    # psycopg は sync/async 同一ドライバなので写像不要（あえて書かない）。
}


def sync_database_url(url: str) -> str:
    """Alembic 用の同期URLへ落とす。

    PostgreSQL(psycopg) はそのまま返る。SQLite の `+aiosqlite` だけが写像される。
    未知の非同期ドライバは**黙って通さず**そのまま返し、接続時に明示的に失敗させる。
    """
    parsed = make_url(url)
    driver = parsed.get_driver_name()
    replacement = SYNC_DRIVER_BY_ASYNC_DRIVER.get(driver)
    if replacement is None:
        return url
    return parsed.set(drivername=f"{parsed.get_backend_name()}+{replacement}").render_as_string(
        hide_password=False
    )
