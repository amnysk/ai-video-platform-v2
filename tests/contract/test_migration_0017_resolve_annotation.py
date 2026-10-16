"""migration 0017 が ``resolve_annotation`` を CHECK に足すこと（0016 は凍結のまま）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from contracts.intervention import CommandType

VERSIONS = Path(__file__).resolve().parents[2] / "infrastructure/db/migrations/versions"
REPO = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, next(VERSIONS.glob(f"{name}_*.py")))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0017_vocabulary_is_0016_plus_resolve_annotation_and_matches_the_contract() -> None:
    m16, m17 = _load("0016"), _load("0017")
    assert tuple(m17.PRE_0017_COMMAND_TYPES) == tuple(m16.INTERVENTION_COMMANDS_TYPE_VALUES)
    assert tuple(member.value for member in CommandType) == m17.COMMAND_TYPES


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_the_database_check_accepts_resolve_annotation_after_0017_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0016")
    engine = sa.create_engine(url)

    def checks() -> str:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name='intervention_commands'")
            ).scalar_one()
        return str(row)

    assert "resolve_annotation" not in checks()
    command.upgrade(_config(url), "0017")
    assert "resolve_annotation" in checks()
    command.downgrade(_config(url), "0016")
    assert "resolve_annotation" not in checks()
    engine.dispose()
