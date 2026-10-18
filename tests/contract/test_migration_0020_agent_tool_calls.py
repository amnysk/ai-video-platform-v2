"""migration 0020（ADR-0047 §6、INV-59）: agent_tool_calls と語彙の張り替え。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from contracts.agent_tools import TOOL_CALL_STATUSES
from contracts.intervention import AgentRole
from contracts.states import ArtifactType

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "infrastructure/db/migrations/versions"
MIGRATION = next(VERSIONS.glob("0020_*.py"))

#: 0019 時点（ADR-0043）の contracts の値。現在の enum から導出しない（downgrade 先は不変）
AT_0019_ARTIFACT_TYPES = [
    "dummy",
    "script",
    "storyboard",
    "scene_image",
    "scene_voice",
    "scene_video",
    "production_manifest",
    "final_video",
    "upload_receipt",
    "scene_visual_override",
    "user_brief",
    "strategy_brief",
    "creative_brief",
    "quality_review",
    "lesson",
    "workflow_plan",
]
AT_0019_AGENT_ROLES = [
    "producer",
    "writer_director",
    "reviewer",
    "strategy",
    "creative_director",
    "quality_reviewer",
    "learning",
]


def _load():
    spec = importlib.util.spec_from_file_location("m0020", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_pre_0020_literals_are_the_values_at_0019() -> None:
    m = _load()
    assert list(m.PRE_0020_ARTIFACT_TYPES) == AT_0019_ARTIFACT_TYPES
    assert list(m.PRE_0020_AGENT_ROLES) == AT_0019_AGENT_ROLES


def test_frozen_literals_equal_the_current_contracts_in_order() -> None:
    m = _load()
    assert list(m.ARTIFACT_TYPES) == [t.value for t in ArtifactType]
    assert list(m.AGENT_ROLES) == [r.value for r in AgentRole]
    assert list(m.TOOL_CALL_STATUSES) == list(TOOL_CALL_STATUSES)


def test_0020_adds_exactly_the_adr_0047_values() -> None:
    m = _load()
    assert set(m.ARTIFACT_TYPES) - set(m.PRE_0020_ARTIFACT_TYPES) == {"fact_check_report"}
    assert set(m.AGENT_ROLES) - set(m.PRE_0020_AGENT_ROLES) == {"historian"}
    assert not set(m.NEW_ARTIFACT_TYPES) & set(m.SCENE_ARTIFACT_TYPES)


def test_the_migration_does_not_import_contracts() -> None:
    import ast

    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in {"contracts", "domain", "infrastructure"}
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"contracts", "domain", "infrastructure"}


def test_the_model_matches_the_migration_columns_and_constraints(tmp_path: Path) -> None:
    from infrastructure.db import models

    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0020")
    engine = sa.create_engine(url)
    insp = sa.inspect(engine)
    migrated = {c["name"]: c["nullable"] for c in insp.get_columns("agent_tool_calls")}
    table = models.AgentToolCallRow.__table__
    assert migrated == {c.name: c.nullable for c in table.columns}
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("agent_tool_calls")}
    assert ("agent_run_key", "seq") in uniques
    engine.dispose()


def _sql(engine: sa.Engine, table: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name=:n"), {"n": table}
            ).scalar_one()
        )


def _index_names(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='artifact_metadata'"
                )
            )
        }


def test_upgrade_and_downgrade_round_trip_on_sqlite(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0019")
    engine = sa.create_engine(url)
    before_indexes = _index_names(engine)
    assert "historian" not in _sql(engine, "agent_runs")

    command.upgrade(_config(url), "0020")
    assert "historian" in _sql(engine, "agent_runs")
    assert "fact_check_report" in _sql(engine, "artifact_metadata")
    assert "agent_tool_calls" in {
        r[0] for r in engine.connect().execute(sa.text("SELECT name FROM sqlite_master"))
    }
    assert _index_names(engine) == before_indexes  # 式索引を失っていない

    command.downgrade(_config(url), "0019")
    assert "historian" not in _sql(engine, "agent_runs")
    assert "fact_check_report" not in _sql(engine, "artifact_metadata")
    assert "agent_tool_calls" not in {
        r[0] for r in engine.connect().execute(sa.text("SELECT name FROM sqlite_master"))
    }
    assert _index_names(engine) == before_indexes
    engine.dispose()


def _insert(conn: sa.Connection, table_name: str, **overrides: object) -> None:
    import datetime as dt

    table = sa.Table(table_name, sa.MetaData(), autoload_with=conn)
    values: dict[str, object] = {}
    for column in table.columns:
        if column.name in overrides or column.nullable:
            continue
        if column.default is not None or column.server_default is not None:
            continue
        kind = column.type
        if isinstance(kind, sa.Uuid) or getattr(kind, "length", None) == 32:
            values[column.name] = uuid.uuid4().hex
        elif isinstance(kind, sa.Integer):
            values[column.name] = 1
        elif isinstance(kind, sa.JSON):
            values[column.name] = []
        elif isinstance(kind, sa.DateTime):
            values[column.name] = dt.datetime.now(dt.UTC)
        elif isinstance(kind, sa.Boolean):
            values[column.name] = False
        else:
            values[column.name] = "x"
    values.update({k: v.hex if isinstance(v, uuid.UUID) else v for k, v in overrides.items()})
    conn.execute(sa.insert(table).values(**values))


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("artifact_metadata", {"artifact_type": "fact_check_report", "sha256": "a" * 64}),
        ("agent_runs", {"role": "historian", "outcome": "succeeded"}),
        (
            "agent_tool_calls",
            {"status": "started", "tool_call_id": str(uuid.uuid4()), "seq": 1},
        ),
    ],
)
def test_downgrade_refuses_and_changes_nothing_when_new_data_is_in_use(
    tmp_path: Path, table: str, values: dict[str, object]
) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0020")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        episode_id = uuid.uuid4()
        _insert(conn, "episodes", id=episode_id, topic="t", status="planned")
        _insert(conn, table, **values, episode_id=episode_id)
    with pytest.raises(RuntimeError, match="refusing to downgrade 0020"):
        command.downgrade(_config(url), "0019")
    assert "historian" in _sql(engine, "agent_runs")
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
        )
        assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1
    engine.dispose()


def test_status_check_rejects_an_unknown_status(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0020")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        episode_id = uuid.uuid4()
        _insert(conn, "episodes", id=episode_id, topic="t", status="planned")
        with pytest.raises(sa.exc.IntegrityError):
            _insert(
                conn,
                "agent_tool_calls",
                status="invented",
                tool_call_id=str(uuid.uuid4()),
                episode_id=episode_id,
            )
    engine.dispose()
