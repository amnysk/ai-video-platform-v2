"""Alembic環境。

接続URLは環境変数 / Settings から取り、alembic.ini に secret を書かない（INV-20）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from infrastructure.db.models import Base
from infrastructure.db.urls import sync_database_url

config = context.config

# CLI 実行時だけ alembic.ini のロギング設定を使う。プログラムから呼ぶ側（テスト等）は
# ``config.attributes["configure_logger"] = False`` で抑止できる。既存ロガーは無効化しない
# （無効化すると import 済みモジュールのログが消え、caplog 依存のテストが順序で落ちる）。
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """優先順位: alembic の -x/ini 指定 > DATABASE_URL > Settings の既定値。

    マイグレーションは同期ドライバで実行する。どのドライバに落とすかの写像は
    ``infrastructure/db/urls.py`` が唯一の定義（ADR-0008）。
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return sync_database_url(configured)

    url = os.environ.get("DATABASE_URL")
    if not url:
        from infrastructure.config import Settings

        url = Settings().database_url

    return sync_database_url(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
