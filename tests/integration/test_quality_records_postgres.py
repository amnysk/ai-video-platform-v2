"""実 PostgreSQL での品質検査の記録（ADR-0081 / INV-126, 129, 130, 132）。docker compose が必要。

専用の使い捨て DB（``*_test``）に **Alembic で** スキーマを作る（追記専用トリガ・24h CHECK・
複合 FK は migration にしか無く、``create_all`` では踏めない）。その上で実の render（エンジンだけ
fake）が作った final_video に対し、flag ON の ``upload_gate`` が「揃った新鮮な記録は許可、古い・
別 hash・欠落は拒否」を実 DB で行うことを確かめる。理由は docs/testing/v3-phase3-quality.md。
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.approval import ApproverKind
from contracts.pipeline import UploadGateRequest
from contracts.quality import MAX_REPORT_VALIDITY, QUALITY_RULE_VERSION
from contracts.quality_records import UsageFeature
from domain.quality.records import CheckSubject
from infrastructure.db.quality_repositories import QualityRecordRepository, SqlQualityRecordWriter
from infrastructure.db.urls import sync_database_url
from infrastructure.quality.service import QualityGateService
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.db import TEST_DB_MARKER, assert_destructive_allowed, require_test_database_url
from tests.support.upload import seed_render_ready
from workers.pipeline.activities import PipelineActivities

REPO = pathlib.Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = require_test_database_url()
PROBE_DATABASE = "avp_quality_records_test"

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture
def probe_url():
    admin_url = sync_database_url(TEST_DATABASE_URL or "")
    url = make_url(admin_url).set(database=PROBE_DATABASE).render_as_string(hide_password=False)
    assert_destructive_allowed(url)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"'))
        conn.execute(text(f'CREATE DATABASE "{PROBE_DATABASE}"'))
        conn.execute(text(f"COMMENT ON DATABASE \"{PROBE_DATABASE}\" IS '{TEST_DB_MARKER}'"))
    yield url
    assert_destructive_allowed(url)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"'))
    admin.dispose()


NEW_TABLES = {
    "quality_check_records",
    "quality_usage_declarations",
    "quality_reports",
    "quality_manual_approvals",
}


def _schema_of_old_tables(url: str) -> dict[str, object]:
    engine = create_engine(url)
    inspector = inspect(engine)
    out: dict[str, object] = {}
    for table in sorted(set(inspector.get_table_names()) - NEW_TABLES - {"alembic_version"}):
        out[table] = (
            [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)],
            sorted(str(c["name"]) for c in inspector.get_check_constraints(table)),
            sorted(str(i["name"]) for i in inspector.get_indexes(table)),
            sorted(str(u["name"]) for u in inspector.get_unique_constraints(table)),
            sorted(str(f["name"]) for f in inspector.get_foreign_keys(table)),
        )
    engine.dispose()
    return out


# --- migration（INV-132）-----------------------------------------------------------------


def test_migration_0019_upgrade_and_downgrade_on_postgres(probe_url: str) -> None:
    config = _config(probe_url)
    command.upgrade(config, "0018")
    before = _schema_of_old_tables(probe_url)
    command.upgrade(config, "0019")
    engine = create_engine(probe_url)
    assert set(inspect(engine).get_table_names()) >= NEW_TABLES
    engine.dispose()
    # 既存の表は列・CHECK・索引・一意・外部キーまで 1 つも変わらない（旧コードが先に動き続ける根拠）
    assert _schema_of_old_tables(probe_url) == before
    command.downgrade(config, "0018")
    engine = create_engine(probe_url)
    assert not NEW_TABLES & set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'quality_records_reject_update'")
        ).scalar_one()
    assert left == 0
    engine.dispose()
    assert _schema_of_old_tables(probe_url) == before


def test_downgrade_refuses_while_audit_rows_exist(probe_url: str) -> None:
    config = _config(probe_url)
    command.upgrade(config, "0019")
    episode_id = uuid.uuid4()
    engine = create_engine(probe_url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status) VALUES (:id, 'planned')"), {"id": episode_id}
        )
        conn.execute(
            text(
                "INSERT INTO quality_usage_declarations (id, episode_id, final_video_sha256, "
                "input_hash, feature, used, declared_by, declared_at) VALUES "
                "(:id, :ep, :sha, :sha, 'bgm', false, 'render', now())"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "sha": "a" * 64},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="refusing to drop"):
        command.downgrade(config, "0018")


# --- DB が構造で守ること（INV-126, 129）------------------------------------------------------


@pytest_asyncio.fixture
async def pg(probe_url: str):
    command.upgrade(_config(probe_url), "head")
    engine = create_async_engine(probe_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _raw(factory, sql: str, **params):
    async with factory() as s:
        await s.execute(text(sql), params)
        await s.commit()


async def _episode(factory) -> uuid.UUID:
    ep = uuid.uuid4()
    await _raw(factory, "INSERT INTO episodes (id, status) VALUES (:id, 'planned')", id=ep)
    return ep


_INSERT_RECORD = (
    "INSERT INTO quality_check_records (id, episode_id, final_video_sha256, input_hash, "
    "rule_version, check_key, generation, verdict, checks, producer, producer_version, "
    "checked_at, expires_at) VALUES (:id, :ep, :sha, :sha, :rule, :key, :gen, 'pass', "
    "CAST('[]' AS json), 'p', '1', :checked, :expires)"
)


def _record_params(ep: uuid.UUID, **over):
    now = datetime.now(UTC)
    params = {
        "id": uuid.uuid4(),
        "ep": ep,
        "sha": "a" * 64,
        "rule": "r",
        "key": "bgm",
        "gen": 1,
        "checked": now,
        "expires": now + timedelta(hours=1),
    }
    params.update(over)
    return params


async def test_records_are_append_only_on_postgres(pg) -> None:
    ep = await _episode(pg)
    params = _record_params(ep)
    await _raw(pg, _INSERT_RECORD, **params)
    with pytest.raises(DBAPIError, match="append-only"):
        await _raw(pg, "UPDATE quality_check_records SET final_video_sha256 = :s", s="b" * 64)
    for table, column in (
        ("quality_usage_declarations", "used"),
        ("quality_reports", "verdict"),
        ("quality_manual_approvals", "approver_id"),
    ):
        async with pg() as s:  # トリガは行が無くても存在する。存在を pg_trigger で確かめる
            names = (
                (
                    await s.execute(
                        text(
                            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                            "WHERE c.relname = :t AND NOT t.tgisinternal"
                        ),
                        {"t": table},
                    )
                )
                .scalars()
                .all()
            )
        assert f"{table}_append_only" in names, (table, column)


async def test_the_database_refuses_an_overlong_validity_and_duplicate_generations(pg) -> None:
    ep = await _episode(pg)
    too_long = _record_params(
        ep, expires=datetime.now(UTC) + MAX_REPORT_VALIDITY + timedelta(hours=1)
    )
    with pytest.raises(IntegrityError):
        await _raw(pg, _INSERT_RECORD, **too_long)
    ok = _record_params(ep)
    await _raw(pg, _INSERT_RECORD, **ok)
    with pytest.raises(IntegrityError):
        await _raw(
            pg, _INSERT_RECORD, **{**ok, "id": uuid.uuid4()}
        )  # 同じ (対象, 規則版, key, 世代)
    await _raw(pg, _INSERT_RECORD, **{**ok, "id": uuid.uuid4(), "gen": 2})
    with pytest.raises(IntegrityError):
        await _raw(pg, _INSERT_RECORD, **_record_params(ep, key="not_a_key"))


async def test_an_approval_cannot_point_at_a_report_for_another_hash(pg) -> None:
    ep = await _episode(pg)
    report_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _raw(
        pg,
        "INSERT INTO quality_reports (id, episode_id, final_video_sha256, input_hash, "
        "rule_version, verdict, generator_version, generated_at, expires_at, report_json, "
        "report_sha256) VALUES "
        "(:id, :ep, :sha, :sha, 'r', 'review', 'g', :g, :e, CAST('{}' AS json), :sha)",
        id=report_id, ep=ep, sha="a" * 64, g=now, e=now + timedelta(hours=1),
    )  # fmt: skip
    insert = (
        "INSERT INTO quality_manual_approvals (id, episode_id, report_id, final_video_sha256, "
        "approved_verdict, approver_kind, approver_id, approved_at) VALUES "
        "(:id, :ep, :rid, :sha, 'review', :kind, :who, now())"
    )
    base = {"id": uuid.uuid4(), "ep": ep, "rid": report_id, "sha": "a" * 64, "kind": "owner"}
    with pytest.raises(IntegrityError):
        await _raw(pg, insert, **{**base, "sha": "b" * 64, "who": "owner-1"})  # 別 hash の報告
    with pytest.raises(IntegrityError):
        await _raw(pg, insert, **{**base, "kind": "someone", "who": "owner-1"})
    with pytest.raises(IntegrityError):
        await _raw(pg, insert, **{**base, "who": ""})
    await _raw(pg, insert, **{**base, "who": "owner-1"})
    assert ApproverKind.OWNER.value == "owner"


# --- 実の final_video 上の gate（INV-126, 127）-----------------------------------------------


class _World:
    def __init__(self, factory, store, episode_id: str, subject: CheckSubject) -> None:
        self.factory = factory
        self.store = store
        self.episode_id = episode_id
        self.subject = subject
        self.now = datetime.now(UTC)

    def acts(self) -> PipelineActivities:
        service = QualityGateService(
            session_factory=self.factory, store=self.store, clock=lambda: self.now
        )
        return PipelineActivities(
            session_factory=self.factory,
            paused_env=False,
            uploads_paused_env=False,
            quality_gate_enforce=True,
            quality_clock=lambda: self.now,
            quality_service=service,
        )

    async def declare(self) -> None:
        writer = SqlQualityRecordWriter(self.factory)
        for feature in UsageFeature:
            await writer.declare_usage(
                self.subject, feature, used=False, declared_by="render", declared_at=self.now
            )

    async def gate(self):
        return await self.acts().upload_gate(UploadGateRequest(self.episode_id))


@pytest_asyncio.fixture
async def world(pg, tmp_path: Path):
    store = InMemoryArtifactStore()
    episode_id = await seed_render_ready(pg, store, tmp_path)
    async with pg() as s:
        current = await QualityRecordRepository(s).current_final_video(episode_id)
    assert current is not None
    subject = CheckSubject(episode_id, current.final_video_sha256, current.input_hash)
    return _World(pg, store, episode_id, subject)


async def test_a_fresh_complete_set_is_admitted_end_to_end(world: _World) -> None:
    await world.declare()
    result = await world.gate()
    assert result.allowed, result.reason
    async with world.factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM quality_check_records"))).scalar_one()
        r = (await s.execute(text("SELECT count(*) FROM quality_reports"))).scalar_one()
    assert (n, r) == (2, 1)


async def test_missing_declarations_are_refused_end_to_end(world: _World) -> None:
    result = await world.gate()
    assert not result.allowed and "quality:" in (result.reason or "")


async def test_stale_and_foreign_records_are_refused_end_to_end(world: _World) -> None:
    # BGM を使ったと宣言し、別 hash / 旧規則版 / 期限切れの BGM 検査だけがある
    writer = SqlQualityRecordWriter(world.factory)
    await writer.declare_usage(
        world.subject, UsageFeature.BGM, used=True, declared_by="render",
        declared_at=world.now, evidence_ref="evidence-1",
    )  # fmt: skip
    await writer.declare_usage(
        world.subject, UsageFeature.MAP, used=False, declared_by="render", declared_at=world.now
    )
    from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
    from contracts.quality_records import CheckKey
    from domain.quality.records import CheckSetDraft

    draft = CheckSetDraft(
        CheckKey.BGM,
        tuple(
            QualityCheck(g, QualityVerdict.PASS, QualityReason.OK, "fixture", True)
            for g in (QualityGate.MUSIC, QualityGate.LICENSE)
        ),
        "fixture",
        "1",
    )
    foreign = CheckSubject(world.episode_id, "f" * 64, world.subject.input_hash)
    await writer.write_check_set(
        foreign, draft, checked_at=world.now, rule_version=QUALITY_RULE_VERSION
    )
    await writer.write_check_set(
        world.subject, draft, checked_at=world.now, rule_version="deterministic-1"
    )
    await writer.write_check_set(
        world.subject,
        draft,
        checked_at=world.now - MAX_REPORT_VALIDITY - timedelta(minutes=1),
        rule_version=QUALITY_RULE_VERSION,
    )
    result = await world.gate()
    assert not result.allowed
    # 正しい新鮮な記録を足せば、同じ gate が許可に変わる（拒否の理由が古さだけだったことの対照）
    await writer.write_check_set(
        world.subject, draft, checked_at=world.now, rule_version=QUALITY_RULE_VERSION
    )
    assert (await world.gate()).allowed
