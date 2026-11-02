"""実 PostgreSQL での BGM カタログ・選曲（ADR-0087 / INV-133, 153）。隔離 harness が必要。

unit（SQLite）では踏めない PostgreSQL 固有の振る舞いを固定する:
  - migration 0019 -> 0021 -> 0022 の upgrade（JSON 列が JSONB になる、CHECK 語彙の拡張）
  - ``save_selection_once`` の同時実行（2 セッションが競っても 1 行、両者が同じ決定を受け取る）
  - ``register_quarantined`` の同じ sha256 は新しい行を作らない
  - ``promote`` のあと ``list_entries`` に承認済みで出る
  - ``scripts/quality_rates.py`` が読み取り専用で走り、行を変えない
理由は docs/testing/ に書く対象（このファイルが各テストの存在理由）。
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.music_selection import DEFAULT_BRAND_POLICY
from domain.music.auto_select import select_for_episode
from domain.music.requirements import derive_requirements
from infrastructure.db.bgm_repositories import SqlBgmCatalogRepository
from infrastructure.db.urls import sync_database_url
from scripts import quality_rates
from tests.support.bgm_auto import entry_for, loud_track, make_script, make_storyboard
from tests.support.bgm_catalog import NOW, make_track
from tests.support.db import TEST_DB_MARKER, assert_destructive_allowed, require_test_database_url

REPO = pathlib.Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = require_test_database_url()
PROBE_DATABASE = "avp_bgm_catalog_test"

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)

BGM_TABLES = {"bgm_tracks", "bgm_selections", "bgm_acquisition_runs", "bgm_acquisition_demands"}
JSON_COLUMNS = {
    "bgm_tracks": {
        "allowed_platforms",
        "allowed_formats",
        "mood_tags",
        "genre_tags",
        "allowed_brands",
        "analysis",
        "inferred_tags",
    },
    "bgm_selections": {"decision"},
    "bgm_acquisition_runs": {"candidates"},
    "bgm_acquisition_demands": {"requirements"},
}


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
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


@pytest_asyncio.fixture
async def factory(probe_url: str):
    command.upgrade(_config(probe_url), "head")
    engine = create_async_engine(probe_url, pool_size=10)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _quarantined(track_id: str = "bgm-q1", sha_char: str = "a", **kw):
    return make_track(
        track_id,
        sha_char,
        status="quarantined",
        license_status="unverified",
        license_evidence_sha256=None,
        license_checked_at=None,
        loudness_lufs=-14.5,
        **kw,
    )


async def _register(repo: SqlBgmCatalogRepository, track):
    return await repo.register_quarantined(
        track,
        acquired_from="owner_supplied",
        acquired_at=NOW,
        evidence_object_key="music/evidence/x.json",
        analysis={"loudness_lufs": -14.5},
        inferred_tags={"mood": ["calm"]},
    )


# --- migration -------------------------------------------------------------------------------


def test_migration_0019_to_0021_to_0022_on_postgres(probe_url: str) -> None:
    config = _config(probe_url)
    command.upgrade(config, "0019")
    engine = create_engine(probe_url)
    with engine.connect() as conn:
        assert not BGM_TABLES & set(
            conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
            .scalars()
            .all()
        )
    command.upgrade(config, "0021")
    with engine.connect() as conn:
        types = {
            (t, c): d
            for t, c, d in conn.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name LIKE 'bgm_%'"
                )
            )
        }
    for table, cols in JSON_COLUMNS.items():
        for col in cols:
            assert types[(table, col)] == "jsonb", (table, col, types[(table, col)])
    command.upgrade(config, "0022")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0022"
        # 0022 が足した語彙を DB が受け入れる（旧語彙も通る）
    with engine.begin() as conn:
        for kind in ("DAILY_WORKFLOW_FAILED", "QUALITY_GATE_HOLD_RATE_HIGH"):
            conn.execute(
                text(
                    "INSERT INTO operational_anomalies (id, kind, anomaly_date, detail, "
                    "first_detected_at, last_detected_at, occurrences) VALUES "
                    "(:id, :k, current_date, CAST('{}' AS json), now(), now(), 1)"
                ),
                {"id": uuid.uuid4(), "k": kind},
            )
        conn.execute(text("DELETE FROM operational_anomalies"))
    engine.dispose()


def test_0021_downgrade_refuses_while_catalog_rows_exist(probe_url: str) -> None:
    config = _config(probe_url)
    command.upgrade(config, "0022")
    engine = create_engine(probe_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bgm_acquisition_demands (demand_sha256, requirements, requested_at) "
                "VALUES (:h, CAST('{}' AS jsonb), now())"
            ),
            {"h": "d" * 64},
        )
    command.downgrade(config, "0021")  # 0022 は語彙の縮小だけ
    with pytest.raises(RuntimeError, match="refusing to drop"):
        command.downgrade(config, "0019")
    engine.dispose()


# --- catalog ---------------------------------------------------------------------------------


async def test_register_quarantined_duplicate_sha_makes_no_new_row(factory) -> None:
    repo = SqlBgmCatalogRepository(factory)
    first, created = await _register(repo, _quarantined("bgm-q1", "a"))
    again, created_again = await _register(repo, _quarantined("bgm-q2-other-id", "a"))
    assert created and not created_again
    assert again.track.track_id == first.track.track_id == "bgm-q1"
    assert len(await repo.list_entries()) == 1


async def test_concurrent_register_same_sha_yields_one_row(factory) -> None:
    repo = SqlBgmCatalogRepository(factory)
    results = await asyncio.gather(
        *(_register(repo, _quarantined(f"bgm-race{i}", "c")) for i in range(6))
    )
    assert sum(1 for _, created in results if created) == 1
    assert len({e.track.track_id for e, _ in results}) == 1
    assert len(await repo.list_entries()) == 1


async def test_promote_then_list_entries_shows_approved_active(factory) -> None:
    repo = SqlBgmCatalogRepository(factory)
    await _register(repo, _quarantined("bgm-q1", "a"))
    approved = make_track(
        "bgm-q1",
        "a",
        loudness_lufs=-14.5,
        license_approver_kind="owner",
        mix_gain_offset_db=6,
        license_checked_at=NOW,
    )
    entry = await repo.promote(approved, verified_at=NOW)
    assert entry.track.status == "active" and entry.track.license_status == "approved"
    listed = await repo.list_entries()
    assert len(listed) == 1
    assert listed[0].track == approved
    assert listed[0].analysis == {"loudness_lufs": -14.5}  # 来歴・実測は promote で消えない
    # 二度目の promote は隔離でないので拒否（承認の上書きをしない）
    with pytest.raises(ValueError, match="not quarantined"):
        await repo.promote(approved, verified_at=NOW)


async def test_concurrent_save_selection_once_one_row_same_decision(factory) -> None:
    repo = SqlBgmCatalogRepository(factory)
    script = make_script()
    req = derive_requirements(
        script, make_storyboard(script), fmt="shorts", brand=DEFAULT_BRAND_POLICY
    )
    episode = str(uuid.uuid4())
    # 競合する 2 つの別の決定（別の曲）。後勝ちしないこと
    d_a = select_for_episode(
        episode,
        req,
        [entry_for(loud_track("bgm-a", "a", mood_tags=(req.mood,)))],
        {},
        NOW,
        brand=DEFAULT_BRAND_POLICY,
    )
    d_b = select_for_episode(
        episode,
        req,
        [entry_for(loud_track("bgm-b", "b", mood_tags=(req.mood,)))],
        {},
        NOW,
        brand=DEFAULT_BRAND_POLICY,
    )
    assert d_a.track_id != d_b.track_id
    decisions = [d_a, d_b] * 4
    got = await asyncio.gather(*(repo.save_selection_once(d, decided_at=NOW) for d in decisions))
    assert len({g.track_id for g in got}) == 1
    async with factory() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM bgm_selections WHERE episode_id = :e"),
                {"e": uuid.UUID(episode)},
            )
        ).scalar_one()
    assert count == 1
    stored = await repo.get_selection(episode)
    assert stored is not None and stored.track_id == got[0].track_id


async def test_finalize_selection_is_once_and_recent_usage_counts_finalized(factory) -> None:
    repo = SqlBgmCatalogRepository(factory)
    script = make_script()
    req = derive_requirements(
        script, make_storyboard(script), fmt="shorts", brand=DEFAULT_BRAND_POLICY
    )
    episode = str(uuid.uuid4())
    d = select_for_episode(
        episode,
        req,
        [entry_for(loud_track("bgm-a", "a", mood_tags=(req.mood,)))],
        {},
        NOW,
        brand=DEFAULT_BRAND_POLICY,
    )
    await repo.save_selection_once(d, decided_at=NOW)
    assert await repo.recent_usage(window=10) == {}  # 未確定は数えない
    results = await asyncio.gather(
        *(repo.finalize_selection(d, finalized_at=NOW + timedelta(seconds=i)) for i in range(4))
    )
    assert len({r.model_dump_json() for r in results}) == 1
    assert await repo.is_selection_finalized(episode)
    assert await repo.recent_usage(window=10) == {"bgm-a": 1}


# --- quality_rates.py (READ ONLY) --------------------------------------------------------------


async def test_quality_rates_is_read_only_against_postgres(factory, probe_url: str) -> None:
    ep = uuid.uuid4()
    now = datetime.now(UTC)
    async with factory() as s:
        await s.execute(
            text("INSERT INTO episodes (id, status) VALUES (:id, 'planned')"), {"id": ep}
        )
        await s.execute(
            text(
                "INSERT INTO quality_reports (id, episode_id, final_video_sha256, input_hash, "
                "rule_version, verdict, generator_version, generated_at, expires_at, "
                "report_json, report_sha256) VALUES (:id, :ep, :h, :h, 'r', 'pass', 'g', :g, "
                ":x, CAST(:j AS json), :h)"
            ),
            {
                "id": uuid.uuid4(),
                "ep": ep,
                "h": "a" * 64,
                "g": now,
                "x": now + timedelta(hours=1),
                "j": '{"checks": []}',
            },
        )
        await s.commit()

    async def _count() -> int:
        async with factory() as s:
            return (await s.execute(text("SELECT count(*) FROM quality_reports"))).scalar_one()

    before = await _count()
    report = await quality_rates.run(probe_url)
    assert report["reports"]["total"] == 1
    assert await _count() == before

    # 同じ接続の設定（READ ONLY）で書くと DB が拒否する
    engine = create_async_engine(probe_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(Exception, match="read-only"):
            await conn.execute(text("DELETE FROM quality_reports"))
    await engine.dispose()
    assert await _count() == before
