"""migration 0019（ADR-0043 §4、INV-54）: 制約だけの張り替え。

理由: docs/testing/jpmas2-p0a-rationale.md
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from contracts.intervention import AgentRole
from contracts.states import ArtifactType, ProviderCall

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "infrastructure/db/migrations/versions"
MIGRATION = next(VERSIONS.glob("0019_*.py"))

#: 9440302（0018 時点）の contracts の値。現在の enum から導出しない（履歴の downgrade 先は不変）
AT_9440302_ARTIFACT_TYPES = [
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
]
AT_9440302_PROVIDER_CALLS = [
    "codex_script",
    "codex_storyboard",
    "fal_image",
    "fal_video",
    "youtube_upload",
    "codex_scene_alternative",
]
AT_9440302_AGENT_ROLES = ["producer", "writer_director", "reviewer"]


def _load():
    spec = importlib.util.spec_from_file_location("m0019", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_pre_0019_literals_are_the_values_at_9440302() -> None:
    m = _load()
    assert list(m.PRE_0019_ARTIFACT_TYPES) == AT_9440302_ARTIFACT_TYPES
    assert list(m.PRE_0019_PROVIDER_CALLS) == AT_9440302_PROVIDER_CALLS
    assert list(m.PRE_0019_AGENT_ROLES) == AT_9440302_AGENT_ROLES


def test_frozen_literals_equal_the_current_enums_in_order() -> None:
    m = _load()
    assert list(m.ARTIFACT_TYPES) == [t.value for t in ArtifactType]
    assert list(m.PROVIDER_CALLS) == [p.value for p in ProviderCall]
    assert list(m.AGENT_ROLES) == [r.value for r in AgentRole]


def test_0019_adds_exactly_the_adr_0043_values() -> None:
    m = _load()
    assert set(m.ARTIFACT_TYPES) - set(m.PRE_0019_ARTIFACT_TYPES) == {
        "user_brief",
        "strategy_brief",
        "creative_brief",
        "quality_review",
        "lesson",
        "workflow_plan",
    }
    assert set(m.PROVIDER_CALLS) - set(m.PRE_0019_PROVIDER_CALLS) == {"agent_llm"}
    assert set(m.AGENT_ROLES) - set(m.PRE_0019_AGENT_ROLES) == {
        "strategy",
        "creative_director",
        "quality_reviewer",
        "learning",
    }


def test_downgrade_restores_every_upgraded_check() -> None:
    m = _load()
    assert {(t, n) for t, n, _ in m.UPGRADED_CHECKS} == {(t, n) for t, n, _ in m.DOWNGRADED_CHECKS}
    assert {n for _, n, _ in m.UPGRADED_CHECKS} == {
        "ck_artifact_metadata_type",
        "ck_artifact_metadata_scene_scope",
        "ck_agent_runs_role",
        "ck_provider_reservations_provider",
        "ck_provider_auth_incidents_provider",
        "ck_provider_rejections_provider",
    }


def test_the_scene_scope_is_unchanged_so_new_artifact_types_are_episode_scoped() -> None:
    from infrastructure.db import models

    m = _load()
    assert {t.value for t in models.SCENE_ARTIFACT_TYPES} == set(m.SCENE_ARTIFACT_TYPES)
    assert not set(m.NEW_ARTIFACT_TYPES) & set(m.SCENE_ARTIFACT_TYPES)


def test_the_migration_does_not_import_contracts() -> None:
    body = MIGRATION.read_text(encoding="utf-8").split('"""', 2)[2]
    assert "contracts" not in body.replace("``contracts", "")  # docstring の言及は除く
    import ast

    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in {"contracts", "domain", "infrastructure"}
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"contracts", "domain", "infrastructure"}


def test_the_migration_creates_no_table_or_column() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    for forbidden in ("create_table", "add_column", "drop_column", "drop_table"):
        assert forbidden not in text


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
    command.upgrade(_config(url), "0018")
    engine = sa.create_engine(url)
    before_indexes = _index_names(engine)
    assert "strategy_brief" not in _sql(engine, "artifact_metadata")
    assert "agent_llm" not in _sql(engine, "provider_reservations")

    command.upgrade(_config(url), "0019")
    for table, word in (
        ("artifact_metadata", "workflow_plan"),
        ("agent_runs", "quality_reviewer"),
        ("provider_reservations", "agent_llm"),
        ("provider_auth_incidents", "agent_llm"),
        ("provider_rejections", "agent_llm"),
    ):
        assert word in _sql(engine, table), table
    assert _index_names(engine) == before_indexes  # 式索引を失っていない

    command.downgrade(_config(url), "0018")
    assert "strategy_brief" not in _sql(engine, "artifact_metadata")
    assert "learning" not in _sql(engine, "agent_runs")
    assert "agent_llm" not in _sql(engine, "provider_reservations")
    assert _index_names(engine) == before_indexes
    engine.dispose()


def _insert(conn: sa.Connection, table_name: str, **overrides: object) -> None:
    """実表を反射し、NOT NULL で既定の無い列を型に合う値で埋めて1行入れる。"""
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
            # SQLite では Uuid は CHAR(32) の hex として反射される
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
        ("artifact_metadata", {"artifact_type": "strategy_brief", "sha256": "a" * 64}),
        ("agent_runs", {"role": "strategy", "outcome": "succeeded"}),
        ("provider_reservations", {"provider": "agent_llm", "status": "reserved"}),
        ("provider_auth_incidents", {"provider": "agent_llm"}),
    ],
)
def test_downgrade_refuses_and_changes_nothing_when_a_new_value_is_in_use(
    tmp_path: Path, table: str, values: dict[str, object]
) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0019")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        episode_id = uuid.uuid4()
        _insert(conn, "episodes", id=episode_id, topic="t", status="planned")
        extra: dict[str, object] = (
            {} if table == "provider_auth_incidents" else {"episode_id": episode_id}
        )
        _insert(conn, table, **values, **extra)
    with pytest.raises(RuntimeError, match="refusing to downgrade 0019"):
        command.downgrade(_config(url), "0018")
    # 何も変わっていない: まだ 0019 の制約で、行も残っている
    assert "agent_llm" in _sql(engine, "provider_reservations")
    assert "workflow_plan" in _sql(engine, "artifact_metadata")
    assert "learning" in _sql(engine, "agent_runs")
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        )
        assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1
    engine.dispose()


def test_downgrade_succeeds_when_only_old_vocabulary_rows_exist(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "0019")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        episode_id = uuid.uuid4()
        _insert(conn, "episodes", id=episode_id, topic="t", status="planned")
        _insert(conn, "agent_runs", role="producer", outcome="succeeded", episode_id=episode_id)
    command.downgrade(_config(url), "0018")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM agent_runs")).scalar_one() == 1
    engine.dispose()
