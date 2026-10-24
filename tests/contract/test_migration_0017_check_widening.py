"""migration 0017 は CHECK 語彙の **拡張** で、旧コードを壊さない（ADR-0060 / INV-82）。

SQLite は ``batch_alter_table`` で表を作り直すので、制約の効き方を実際の INSERT で確かめる
（実 PostgreSQL 版は tests/integration/test_migration_against_postgres.py）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from contracts.schedule_guard import AnomalyKind
from domain import release_preflight as rp

REPO = pathlib.Path(__file__).resolve().parents[2]
FILE = REPO / "infrastructure/db/migrations/versions/0017_daily_workflow_failure_kinds.py"


def _widening() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("m0017", FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CHECK_WIDENING


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture
def at_0016(tmp_path: pathlib.Path) -> tuple[Engine, Config]:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    config = _config(url)
    command.upgrade(config, "0016")
    engine = create_engine(url)
    _as_production_at_0015(engine)
    return engine, config


def _as_production_at_0015(engine: Engine) -> None:
    """本番 DB の形にする。

    0012 は ``for k in AnomalyKind`` で CHECK を作る（凍結されていない）ので、**新規 DB は
    0012 の時点でもう新しい語彙を持つ**。本番は 0012 を旧 enum で適用済みなので旧集合のまま。
    0012 は本番に入った migration で書き換えられない（preflight が BLOCKED にする）ため、
    ここで本番の形を再現する。
    """
    old = _widening()["old"]
    values = ", ".join(f"'{v}'" for v in old)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        with ops.batch_alter_table("operational_anomalies") as batch:
            batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
            batch.create_check_constraint("ck_operational_anomalies_kind", f"kind IN ({values})")


def _insert(engine: Engine, kind: str, day: int = 1) -> None:
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO operational_anomalies (id, kind, anomaly_date, episode_id, detail,"
                " first_detected_at, last_detected_at, occurrences)"
                " VALUES (:id, :k, :d, NULL, '{}', :t, :t, 1)"
            ),
            {"id": uuid.uuid4().hex, "k": kind, "d": date(2026, 10, day).isoformat(), "t": now},
        )


def _accepts(engine: Engine, kind: str) -> bool:
    try:
        _insert(engine, kind, day=2)
    except IntegrityError:
        return False
    return True


def test_declared_old_set_is_what_0016_actually_accepts(at_0016: tuple[Engine, Config]) -> None:
    """宣言の ``old`` を信じる根拠: 実際に 0016 時点の制約が受ける集合と一致する。"""
    engine, _ = at_0016
    spec = _widening()
    for kind in spec["old"]:
        assert _accepts(engine, kind), kind
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM operational_anomalies"))
    for kind in spec["new"]:
        if kind not in spec["old"]:
            assert not _accepts(engine, kind), kind


def test_new_set_is_every_AnomalyKind_the_code_can_write() -> None:
    assert set(_widening()["new"]) == {k.value for k in AnomalyKind}


def test_after_upgrade_old_kinds_still_work_new_kinds_work_unknown_still_rejected(
    at_0016: tuple[Engine, Config],
) -> None:
    """旧コードの書く値は全て通り続ける（expand-only）。拡張は宣言した値だけ。"""
    engine, config = at_0016
    spec = _widening()
    command.upgrade(config, "0017")
    for kind in spec["old"]:
        assert _accepts(engine, kind), f"old code's kind {kind} must keep working"
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM operational_anomalies"))
    for kind in spec["new"]:
        assert _accepts(engine, kind), kind
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM operational_anomalies"))
    assert not _accepts(engine, "NOT_A_KIND")
    assert not _accepts(engine, "")


def test_rows_written_by_old_code_survive_the_upgrade(at_0016: tuple[Engine, Config]) -> None:
    engine, config = at_0016
    for i, kind in enumerate(_widening()["old"], start=1):
        _insert(engine, kind, day=i)
    command.upgrade(config, "0017")
    with engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM operational_anomalies")).scalar_one()
    assert n == len(_widening()["old"])


def test_downgrade_removes_only_new_kind_rows_and_restores_the_old_constraint(
    at_0016: tuple[Engine, Config],
) -> None:
    engine, config = at_0016
    spec = _widening()
    command.upgrade(config, "0017")
    _insert(engine, spec["old"][0], day=1)
    for i, kind in enumerate(k for k in spec["new"] if k not in spec["old"]):
        _insert(engine, kind, day=10 + i)
    command.downgrade(config, "0016")
    with engine.begin() as conn:
        kinds = [r[0] for r in conn.execute(text("SELECT kind FROM operational_anomalies"))]
    assert kinds == [spec["old"][0]]
    for kind in spec["new"]:
        if kind not in spec["old"]:
            assert not _accepts(engine, kind)
    command.upgrade(config, "0017")  # 往復できる


def test_the_release_scanner_accepts_this_migration_as_a_widening_and_nothing_else() -> None:
    src = FILE.read_text()
    assert rp.scan_migration(src) == []
    assert len(rp.check_widenings(src)) == 1
