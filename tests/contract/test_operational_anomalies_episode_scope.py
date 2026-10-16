"""operational_anomalies の Episode 単位の粒度（ADR-0031）。

migration 0012 が作る2本の部分インデックスが、モデル定義と一致し、実際に
「schedule系は1日1行」「Episode系はEpisodeごとに1日1行」を強制することを検査する。
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, date, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import IntegrityError

from infrastructure.db.models import OperationalAnomalyRow

REPO = pathlib.Path(__file__).resolve().parents[2]


def _upgraded_engine(tmp_path: pathlib.Path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return create_engine(url)


def _row(*, kind: str, anomaly_date: date, episode_id: uuid.UUID | None) -> dict:
    now = datetime.now(UTC)
    return {
        "id": uuid.uuid4(),
        "kind": kind,
        "anomaly_date": anomaly_date,
        "episode_id": episode_id,
        "detail": {},
        "first_detected_at": now,
        "last_detected_at": now,
        "occurrences": 1,
    }


def test_two_schedule_level_rows_same_kind_and_date_collide(tmp_path: pathlib.Path) -> None:
    engine = _upgraded_engine(tmp_path)
    d = date(2026, 9, 23)
    with engine.begin() as conn:
        conn.execute(
            insert(OperationalAnomalyRow),
            [_row(kind="SCHEDULE_MISSING", anomaly_date=d, episode_id=None)],
        )
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(OperationalAnomalyRow),
                [_row(kind="SCHEDULE_MISSING", anomaly_date=d, episode_id=None)],
            )
        raise AssertionError("expected the schedule-level unique index to reject the duplicate")
    except IntegrityError:
        pass


def test_two_episodes_same_kind_and_date_do_not_collide(tmp_path: pathlib.Path) -> None:
    """ADR-0031 の核心: 同日に複数の Episode が同じ kind で異常を出しても取りこぼさない。"""
    engine = _upgraded_engine(tmp_path)
    d = date(2026, 9, 23)
    ep1, ep2 = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(OperationalAnomalyRow),
            [
                _row(kind="EPISODE_STAGE_STALLED", anomaly_date=d, episode_id=ep1),
                _row(kind="EPISODE_STAGE_STALLED", anomaly_date=d, episode_id=ep2),
            ],
        )
    with engine.connect() as conn:
        rows = conn.execute(
            select(OperationalAnomalyRow.episode_id).where(
                OperationalAnomalyRow.kind == "EPISODE_STAGE_STALLED"
            )
        ).all()
    assert {r[0] for r in rows} == {str(ep1), str(ep2)} or {r[0] for r in rows} == {ep1, ep2}


def test_the_same_episode_twice_same_kind_and_date_collides(tmp_path: pathlib.Path) -> None:
    engine = _upgraded_engine(tmp_path)
    d = date(2026, 9, 23)
    ep = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(OperationalAnomalyRow),
            [_row(kind="EPISODE_STAGE_STALLED", anomaly_date=d, episode_id=ep)],
        )
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(OperationalAnomalyRow),
                [_row(kind="EPISODE_STAGE_STALLED", anomaly_date=d, episode_id=ep)],
            )
        raise AssertionError("expected the episode-level unique index to reject the duplicate")
    except IntegrityError:
        pass


def test_downgrade_with_episode_scoped_rows_does_not_crash(tmp_path: pathlib.Path) -> None:
    """0012 の downgrade は、旧 CHECK が表現できない行が残っていても壊れずに戻る。

    旧制約を先に戻すと CHECK 違反で downgrade 自体が失敗し、_alembic_tmp_* が残る
    壊れたスキーマになっていた（独立レビューで再現）。episode 単位の行を消してから
    旧制約に戻す。
    """
    engine = _upgraded_engine(tmp_path)
    d = date(2026, 9, 23)
    with engine.begin() as conn:
        conn.execute(
            insert(OperationalAnomalyRow),
            [_row(kind="EPISODE_STAGE_STALLED", anomaly_date=d, episode_id=uuid.uuid4())],
        )

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    config.attributes["configure_logger"] = False
    command.downgrade(config, "0011")  # 例外を出さずに戻り切ること

    # downgrade 後は ORM モデル（現行スキーマ）と実テーブル（旧スキーマ）が一致しないので、
    # 素の SQL で確認する: episode_id 列が無く、行も残っていないこと（想定通り失われる）
    with engine.connect() as conn:
        info = conn.exec_driver_sql("PRAGMA table_info(operational_anomalies)")
        assert "episode_id" not in {row[1] for row in info}
        (count,) = conn.exec_driver_sql("SELECT COUNT(*) FROM operational_anomalies").one()
        assert count == 0

    # 旧 CHECK が実際に有効であること（新しい4種の kind はもう入らない）
    insert_old_kind = (
        "INSERT INTO operational_anomalies "
        "(id, kind, anomaly_date, detail, first_detected_at, last_detected_at, occurrences) "
        "VALUES ('x', 'EPISODE_STAGE_STALLED', :d, '{}', :now, :now, 1)"
    )
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                insert_old_kind, {"d": d.isoformat(), "now": datetime.now(UTC).isoformat()}
            )
        raise AssertionError("expected the old CHECK constraint to reject a post-0012 kind")
    except IntegrityError:
        pass
