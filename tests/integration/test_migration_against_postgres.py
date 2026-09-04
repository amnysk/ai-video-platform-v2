"""実PostgreSQLに対する Alembic マイグレーション検査。

tests/contract/test_migration_matches_models.py は **SQLite** に対して走るので、
(1) PostgreSQL 固有の DDL 差異 と (2) 同期ドライバの欠落 のどちらも検出できない。
psycopg2 未導入で migrate コンテナが落ちた事故は、まさにこの穴だった。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from infrastructure.db.models import Base
from infrastructure.db.urls import sync_database_url

REPO = pathlib.Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in DATABASE_URL,
    reason="DATABASE_URL must point at PostgreSQL (docker compose --profile core up -d)",
)


def _sync_url() -> str:
    """env.py と同じ関数で同期URLを得る。ここで独自に組み立て直さない。"""
    return sync_database_url(DATABASE_URL)


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


PROBE_DATABASE = "avp_alembic_probe"


@pytest.fixture
def probe_url():
    """マイグレーション専用の使い捨てDB。開発スタックのデータを壊さない。

    alembic.ini は configparser なので URL に `%` を含められない
    （`?options=-csearch_path%3D...` は補間構文として弾かれる）。
    そのため schema ではなく database を分ける。
    """
    admin_url = _sync_url()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"'))
        conn.execute(text(f'CREATE DATABASE "{PROBE_DATABASE}"'))

    url = make_url(admin_url).set(database=PROBE_DATABASE).render_as_string(hide_password=False)
    yield url

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"'))
    admin.dispose()


def test_alembic_upgrade_head_succeeds_on_real_postgres(probe_url) -> None:
    """同期ドライバが実際に import できることも、ここで初めて検証される。"""
    command.upgrade(_config(probe_url), "head")

    engine = create_engine(probe_url)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    engine.dispose()


def test_alembic_downgrade_base_succeeds_on_real_postgres(probe_url) -> None:
    config = _config(probe_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(probe_url)
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()
    engine.dispose()


def test_check_constraints_survive_on_postgres(probe_url) -> None:
    """CHECK制約はPostgreSQLでのみ実効。SQLiteのテストでは踏めない。"""
    command.upgrade(_config(probe_url), "head")
    engine = create_engine(probe_url)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'public' AND c.contype = 'c'"
            )
        ).scalars()
        names = set(rows)
    engine.dispose()

    for expected in ("ck_episodes_status", "ck_jobs_status", "ck_artifact_metadata_type"):
        assert expected in names, f"{expected} が実PostgreSQLに作られていない"
