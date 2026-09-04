"""Alembicマイグレーションと ORM モデルの突き合わせ。

「片側だけ更新する」ことがこのプロジェクトの事故の原型なので、
マイグレーションを適用したDBの実形状と、モデル定義を機械で比較する。
"""

from __future__ import annotations

import pathlib

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from infrastructure.db.models import Base

REPO = pathlib.Path(__file__).resolve().parents[2]


def _upgraded_engine(tmp_path: pathlib.Path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return create_engine(url)


def test_migration_creates_exactly_the_modelled_tables(tmp_path: pathlib.Path) -> None:
    engine = _upgraded_engine(tmp_path)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)


def test_migration_columns_match_the_models(tmp_path: pathlib.Path) -> None:
    engine = _upgraded_engine(tmp_path)
    inspector = inspect(engine)
    problems: list[str] = []
    for name, table in Base.metadata.tables.items():
        migrated = {c["name"] for c in inspector.get_columns(name)}
        modelled = {c.name for c in table.columns}
        if migrated != modelled:
            problems.append(f"{name}: migration={sorted(migrated)} models={sorted(modelled)}")
    assert not problems, "\n".join(problems)


def test_downgrade_removes_the_tables(tmp_path: pathlib.Path) -> None:
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    remaining = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert remaining == set()
