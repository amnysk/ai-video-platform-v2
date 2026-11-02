"""``scripts/quality_rates.py``: 読み取り専用の判定率の集計（ADR-0087 §11）。sqlite で検証する。"""

from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from domain.quality.deterministic import QualityInputs, build_deterministic_report
from infrastructure.db.models import Base, EpisodeRow, QualityReportRow
from infrastructure.db.quality_repositories import QualityRecordRepository
from tests.support.script_loader import load_script_module

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "quality_rates.py"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def cli():
    return load_script_module("quality_rates_script", SCRIPT)


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    path = tmp_path / "rates.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False), path
    await engine.dispose()


async def _seed(factory, minute: int, verdict: QualityVerdict, reason: QualityReason, ep=None):
    episode_id = ep or str(uuid.uuid4())
    async with factory() as s:
        if await s.get(EpisodeRow, uuid.UUID(episode_id)) is None:
            s.add(EpisodeRow(id=uuid.UUID(episode_id), topic="t", status="render_ready"))
            await s.commit()
    check = QualityCheck(QualityGate.AUDIO, verdict, reason, "fixed detail", True)
    report = build_deterministic_report(
        QualityInputs(episode_id, f"{minute:064x}", "b" * 64),
        now=T0 + timedelta(minutes=minute),
        rule_version="r1",
        providers=(lambda _i: [check],),
    )
    async with factory() as s:
        await QualityRecordRepository(s).save_report(report)
        await s.commit()
    return episode_id


async def test_rates_split_quality_holds_from_inspection_unavailable(cli, db) -> None:
    factory, path = db
    P, W, R, B = (
        (QualityVerdict.PASS, QualityReason.OK),
        (QualityVerdict.WARN, QualityReason.MINOR_WORDING),
        (QualityVerdict.REVIEW, QualityReason.LICENSE_UNVERIFIED),
        (QualityVerdict.BLOCK, QualityReason.AUDIO_DEFECT),
    )
    U = (QualityVerdict.REVIEW, QualityReason.INSPECTION_UNAVAILABLE)
    for i, (v, r) in enumerate([P, P, P, W, R, R, B, U, U, U]):
        await _seed(factory, i, v, r)
    out = await cli.run(f"sqlite:///{path}")
    rep = out["reports"]
    assert (rep["total"], rep["pass"], rep["warn"], rep["review"], rep["block"]) == (10, 3, 1, 2, 1)
    assert rep["inspection_unavailable"] == 3
    assert rep["upload_allowed"] == 4 and rep["upload_allowed_rate"] == 0.4
    assert rep["inspection_unavailable_rate"] == 0.3
    # 直近 10 件のうち保留は 6
    assert out["recent_hold_alarm"]["holds"] == 6
    assert out["recent_hold_alarm"]["inspection_holds"] == 3


async def test_latest_per_episode_counts_each_episode_once(cli, db) -> None:
    factory, path = db
    ep = await _seed(factory, 0, QualityVerdict.REVIEW, QualityReason.INSPECTION_UNAVAILABLE)
    await _seed(factory, 1, QualityVerdict.PASS, QualityReason.OK, ep=ep)
    out = await cli.run(f"sqlite:///{path}")
    assert out["reports"]["total"] == 2
    assert out["latest_per_episode"]["total"] == 1
    assert out["latest_per_episode"]["pass"] == 1


async def test_empty_database_has_null_rates_and_no_alarm(cli, db) -> None:
    _, path = db
    out = await cli.run(f"sqlite:///{path}")
    assert out["reports"]["total"] == 0 and out["reports"]["upload_allowed_rate"] is None
    assert out["recent_hold_alarm"] is None


async def test_the_script_never_writes(cli, db) -> None:
    factory, path = db
    await _seed(factory, 0, QualityVerdict.PASS, QualityReason.OK)
    async with factory() as s:
        before = (await s.execute(select(func.count()).select_from(QualityReportRow))).scalar_one()
    await cli.run(f"sqlite:///{path}")
    async with factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(QualityReportRow))
        ).scalar_one() == before


def test_the_script_source_contains_only_selects_and_read_only_transactions() -> None:
    src = SCRIPT.read_text()
    upper = src.upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", ".COMMIT("):
        assert forbidden not in upper, forbidden
    assert "SET TRANSACTION READ ONLY" in upper


def test_a_database_url_is_required(cli) -> None:
    with pytest.raises(SystemExit):
        import asyncio

        asyncio.run(cli.main([]))


def test_url_normalisation(cli) -> None:
    assert cli.normalize_url("postgresql://u@h/d") == "postgresql+asyncpg://u@h/d"
    assert cli.normalize_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert cli.normalize_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"
