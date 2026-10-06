"""migration 0015（research の永続化。ADR-0037）を実際に適用した SQLite で検査する。

- 語彙は literal で凍結し、contracts を import しない（0014 と同じ規約）
- DB 制約そのものが INV-36（呼び出し番号の一意・正数）と「現行は1本」を強制する
- 本番の表（jobs / artifact_metadata / provider_reservations / provider_rejections）に触れない
- downgrade は research の行が残っていれば何も変えずに拒否する

理由は docs/testing/research-persistence-rationale.md。
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import UTC, datetime
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION_0015 = REPO / "infrastructure/db/migrations/versions/0015_research_foundation.py"
RESEARCH_TABLES = {"research_requests", "research_calls", "research_artifacts"}
PRODUCTION_TABLES = ("jobs", "artifact_metadata", "provider_reservations", "provider_rejections")

#: ADR-0037 で凍結した語彙（定義順）。contracts/research.py から導出しない。
FROZEN_KINDS = ["trend", "evidence"]
FROZEN_STATUSES = ["queued", "running", "completed", "partial", "blocked", "failed"]
FROZEN_CALLS = ["search", "fetch", "assess"]
FROZEN_CALL_STATUSES = ["reserved", "spent", "abandoned"]
FROZEN_ARTIFACT_TYPES = ["research_trend", "research_evidence", "research_script_verification"]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0015", MIGRATION_0015)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


def _upgraded(tmp_path: pathlib.Path) -> tuple[Config, Engine]:
    url = f"sqlite:///{tmp_path / 'research.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    return config, create_engine(url)


def _insert_request(conn, *, key: str = "k1") -> uuid.UUID:
    request_id = uuid.uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO research_requests (id, idempotency_key, request_hash, kind, status, "
            "requester, channel_id, payload, limits, policy_version, schema_version, "
            "provider_config_version, as_of, created_at, updated_at) VALUES (:id, :key, :hash, "
            "'evidence', 'running', 'script', 'c', '{}', '{}', 'p', '1.0', 'pc', :now, :now, :now)"
        ),
        {"id": request_id.hex, "key": key, "hash": "0" * 64, "now": now.isoformat()},
    )
    return request_id


def _insert_call(conn, request_id: uuid.UUID, seq: int, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": uuid.uuid4().hex,
        "request_id": request_id.hex,
        "provider_call": "search",
        "call_seq": seq,
        "idempotency_key": f"key-{uuid.uuid4()}",
        "input_hash": "0" * 64,
        "provider": "fake",
        "status": "reserved",
        "reserved_at": datetime.now(UTC).isoformat(),
        "dispatched_at": None,
        "settled_at": None,
    }
    values.update(overrides)
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    conn.execute(text(f"INSERT INTO research_calls ({columns}) VALUES ({params})"), values)


def _insert_artifact(conn, request_id: uuid.UUID, sha: str, *, superseded: bool = False) -> None:
    conn.execute(
        text(
            "INSERT INTO research_artifacts (id, request_id, artifact_type, schema_version, "
            "bucket, object_key, sha256, size_bytes, version, created_at, superseded_at) "
            "VALUES (:id, :rid, 'research_evidence', '1.0', 'b', :key, :sha, 1, :version, :now, "
            ":superseded)"
        ),
        {
            "id": uuid.uuid4().hex,
            "rid": request_id.hex,
            "key": f"research/{request_id}/research_evidence/{sha}.json",
            "sha": sha,
            "version": int(sha[0], 16) + 1,
            "now": datetime.now(UTC).isoformat(),
            "superseded": datetime.now(UTC).isoformat() if superseded else None,
        },
    )


# ------------------------------------------------------------------ 規約


def test_0015_follows_0014_and_freezes_its_vocabulary_as_literals() -> None:
    migration = _load()
    assert (migration.revision, migration.down_revision) == ("0015", "0014")
    assert list(migration.RESEARCH_KINDS) == FROZEN_KINDS
    assert list(migration.RESEARCH_STATUSES) == FROZEN_STATUSES
    assert list(migration.RESEARCH_CALLS) == FROZEN_CALLS
    assert list(migration.RESEARCH_CALL_STATUSES) == FROZEN_CALL_STATUSES
    assert list(migration.RESEARCH_ARTIFACT_TYPES) == FROZEN_ARTIFACT_TYPES
    source = MIGRATION_0015.read_text(encoding="utf-8")
    assert "from contracts" not in source and "import contracts" not in source


def test_0015_vocabulary_matches_the_contracts_it_was_written_for() -> None:
    from contracts.research import (
        ResearchArtifactType,
        ResearchCall,
        ResearchCallStatus,
        ResearchKind,
        ResearchStatus,
    )

    assert [k.value for k in ResearchKind] == FROZEN_KINDS
    assert [s.value for s in ResearchStatus] == FROZEN_STATUSES
    assert [c.value for c in ResearchCall] == FROZEN_CALLS
    assert [s.value for s in ResearchCallStatus] == FROZEN_CALL_STATUSES
    assert [a.value for a in ResearchArtifactType] == FROZEN_ARTIFACT_TYPES


def test_upgrade_adds_only_research_tables_and_leaves_production_tables_alone(
    tmp_path: pathlib.Path,
) -> None:
    before_url = f"sqlite:///{tmp_path / 'before.db'}"
    command.upgrade(_config(before_url), "0014")
    before = inspect(create_engine(before_url))
    before_tables = set(before.get_table_names())
    before_shape = {
        t: (
            sorted(c["name"] for c in before.get_columns(t)),
            sorted(str(c["sqltext"]) for c in before.get_check_constraints(t)),
        )
        for t in PRODUCTION_TABLES
    }
    _config_after, engine = _upgraded(tmp_path)
    after = inspect(engine)
    assert set(after.get_table_names()) - before_tables == RESEARCH_TABLES
    for table in PRODUCTION_TABLES:
        shape = (
            sorted(c["name"] for c in after.get_columns(table)),
            sorted(str(c["sqltext"]) for c in after.get_check_constraints(table)),
        )
        assert shape == before_shape[table], table


def test_research_tables_only_reference_research_tables(tmp_path: pathlib.Path) -> None:
    """INV-37: research の行は本番の表を FK で指さない（Episode は id の参照だけ）。"""
    _config_after, engine = _upgraded(tmp_path)
    inspector = inspect(engine)
    for table in RESEARCH_TABLES:
        for fk in inspector.get_foreign_keys(table):
            assert fk["referred_table"] in RESEARCH_TABLES, (table, fk)


# ------------------------------------------------------------------ INV-36 の DB 制約


def test_the_database_rejects_a_second_row_with_the_same_call_seq(tmp_path: pathlib.Path) -> None:
    _config_after, engine = _upgraded(tmp_path)
    with engine.begin() as conn:
        request_id = _insert_request(conn)
        _insert_call(conn, request_id, 1)
        _insert_call(conn, request_id, 1, provider_call="fetch")  # 種別が違えば別の枠
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_call(conn, request_id, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"call_seq": 0},
        {"provider_call": "search_web"},
        {"status": "released"},
        {"status": "abandoned", "dispatched_at": "2026-09-29T00:00:00+00:00"},
        {"status": "spent"},  # settled_at が無い
        {"estimated_cost_usd": -1},
        {"quota_units": -1},
    ],
)
def test_the_database_rejects_invalid_ledger_rows(
    tmp_path: pathlib.Path, overrides: dict[str, object]
) -> None:
    _config_after, engine = _upgraded(tmp_path)
    with engine.begin() as conn:
        request_id = _insert_request(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_call(conn, request_id, 1, **overrides)


def test_the_database_keeps_one_current_artifact_per_type(tmp_path: pathlib.Path) -> None:
    _config_after, engine = _upgraded(tmp_path)
    with engine.begin() as conn:
        request_id = _insert_request(conn)
        _insert_artifact(conn, request_id, "a" * 64, superseded=True)
        _insert_artifact(conn, request_id, "b" * 64)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_artifact(conn, request_id, "c" * 64)


def test_the_database_rejects_unknown_request_vocabulary(tmp_path: pathlib.Path) -> None:
    _config_after, engine = _upgraded(tmp_path)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        request_id = _insert_request(conn)
        conn.execute(
            text("UPDATE research_requests SET kind = 'strategy' WHERE id = :id"),
            {"id": request_id.hex},
        )


# ------------------------------------------------------------------ downgrade


def test_downgrade_drops_the_empty_research_tables(tmp_path: pathlib.Path) -> None:
    config, engine = _upgraded(tmp_path)
    command.downgrade(config, "0014")
    assert not RESEARCH_TABLES & set(inspect(engine).get_table_names())


def test_downgrade_refuses_while_research_rows_exist(tmp_path: pathlib.Path) -> None:
    config, engine = _upgraded(tmp_path)
    with engine.begin() as conn:
        _insert_request(conn)
    with pytest.raises(RuntimeError, match="research"):
        command.downgrade(config, "0014")
    engine.dispose()
    assert set(inspect(create_engine(engine.url)).get_table_names()) >= RESEARCH_TABLES
