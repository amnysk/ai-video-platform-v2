"""Alembic環境。

接続URLは環境変数 / Settings から取り、alembic.ini に secret を書かない（INV-20）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from infrastructure.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """優先順位: alembic の -x/ini 指定 > DATABASE_URL > Settings の既定値。"""
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    url = os.environ.get("DATABASE_URL")
    if not url:
        from infrastructure.config import Settings

        url = Settings().database_url

    # マイグレーションは同期ドライバで実行する。
    return url.replace("+asyncpg", "").replace("+aiosqlite", "")


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
