"""migration 0018: agent_runs / decision_records に Scene・Temporal 識別子（NULL 可）を足す。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO = Path(__file__).resolve().parents[2]

AGENT_RUN_NEW = {"workflow_id", "workflow_run_id", "activity_id"}
DECISION_NEW = {"scene_id", "workflow_id", "workflow_run_id"}


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine, table: str) -> dict[str, bool]:
    return {c["name"]: bool(c["nullable"]) for c in sa.inspect(engine).get_columns(table)}


def _indexes(engine: sa.Engine, table: str) -> dict[str, list[str]]:
    return {i["name"]: list(i["column_names"]) for i in sa.inspect(engine).get_indexes(table)}


def test_0018_adds_nullable_columns_and_indexes_and_downgrade_removes_them(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0017")
    engine = sa.create_engine(url)
    assert not AGENT_RUN_NEW & set(_columns(engine, "agent_runs"))
    assert not DECISION_NEW & set(_columns(engine, "decision_records"))

    command.upgrade(_config(url), "0018")
    engine.dispose()
    engine = sa.create_engine(url)
    runs, decisions = _columns(engine, "agent_runs"), _columns(engine, "decision_records")
    assert all(runs[name] for name in AGENT_RUN_NEW)  # nullable
    assert all(decisions[name] for name in DECISION_NEW)
    assert _indexes(engine, "decision_records")["ix_decision_records_episode_scene"] == [
        "episode_id",
        "scene_id",
    ]
    assert _indexes(engine, "agent_runs")["ix_agent_runs_workflow"] == ["workflow_id"]

    command.downgrade(_config(url), "0017")
    engine.dispose()
    engine = sa.create_engine(url)
    assert not AGENT_RUN_NEW & set(_columns(engine, "agent_runs"))
    assert not DECISION_NEW & set(_columns(engine, "decision_records"))
    assert "ix_agent_runs_workflow" not in _indexes(engine, "agent_runs")
    engine.dispose()
