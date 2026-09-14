"""実PostgreSQLに対する Alembic マイグレーション検査。

tests/contract/test_migration_matches_models.py は **SQLite** に対して走るので、
(1) PostgreSQL 固有の DDL 差異 と (2) 同期ドライバの欠落 のどちらも検出できない。
psycopg2 未導入で migrate コンテナが落ちた事故は、まさにこの穴だった。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from infrastructure.db.models import Base
from infrastructure.db.urls import sync_database_url

REPO = pathlib.Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in DATABASE_URL,
    reason="DATABASE_URL must point at PostgreSQL (docker compose --profile core up -d)",
)


def _sync_url() -> str:
    """env.py と同じ関数で同期URLを得る。ここで独自に組み立て直さない。"""
    return sync_database_url(DATABASE_URL)


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


PROBE_DATABASE = "avp_alembic_probe"


@pytest.fixture
def probe_url():
    """マイグレーション専用の使い捨てDB。開発スタックのデータを壊さない。

    alembic.ini は configparser なので URL に `%` を含められない
    （`?options=-csearch_path%3D...` は補間構文として弾かれる）。
    そのため schema ではなく database を分ける。
    """
    admin_url = _sync_url()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"'))
        conn.execute(text(f'CREATE DATABASE "{PROBE_DATABASE}"'))

    url = make_url(admin_url).set(database=PROBE_DATABASE).render_as_string(hide_password=False)
    yield url

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"'))
    admin.dispose()


def test_alembic_upgrade_head_succeeds_on_real_postgres(probe_url) -> None:
    """同期ドライバが実際に import できることも、ここで初めて検証される。"""
    command.upgrade(_config(probe_url), "head")

    engine = create_engine(probe_url)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    engine.dispose()


def test_alembic_downgrade_base_succeeds_on_real_postgres(probe_url) -> None:
    config = _config(probe_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(probe_url)
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()
    engine.dispose()


def test_check_constraints_survive_on_postgres(probe_url) -> None:
    """CHECK制約はPostgreSQLでのみ実効。SQLiteのテストでは踏めない。"""
    command.upgrade(_config(probe_url), "head")
    engine = create_engine(probe_url)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'public' AND c.contype = 'c'"
            )
        ).scalars()
        names = set(rows)
    engine.dispose()

    for expected in ("ck_episodes_status", "ck_jobs_status", "ck_artifact_metadata_type"):
        assert expected in names, f"{expected} が実PostgreSQLに作られていない"


def test_storyboard_vocabulary_is_accepted_by_postgres_checks(probe_url) -> None:
    """0003: storyboard の語彙が実PostgreSQLの CHECK を通り、downgrade で再び拒否される。"""
    import uuid

    from sqlalchemy.exc import IntegrityError

    config = _config(probe_url)
    command.upgrade(config, "head")
    engine = create_engine(probe_url)
    episode_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'storyboard_ready', 't')"),
            {"id": episode_id},
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, episode_id, type, status, attempts, max_attempts) "
                "VALUES (:id, :ep, 'plan_storyboard', 'queued', 0, 1)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id},
        )
        conn.execute(
            text(
                "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, "
                "bucket, object_key, sha256, input_hash, version) VALUES "
                "(:id, :ep, 'storyboard', '1.0', 'b', 'k', :sha, :sha, 1)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "sha": "a" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO provider_reservations (id, episode_id, provider, idempotency_key, "
                "input_hash, round, status) VALUES "
                "(:id, :ep, 'codex_storyboard', :key, :key, 1, 'reserved')"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "key": "k" * 64},
        )
        conn.execute(text("DELETE FROM episodes"))
    engine.dispose()

    command.downgrade(config, "0002")
    engine = create_engine(probe_url)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'storyboard_ready', 't')"),
            {"id": uuid.uuid4()},
        )
    engine.dispose()


def test_production_vocabulary_and_scene_keys_on_postgres(probe_url) -> None:
    """0004: Phase 4 の語彙と scene キーの一意性が実PostgreSQLで効く。

    downgrade は Phase 4 の行が残ると失敗する。
    """
    import uuid

    from sqlalchemy.exc import IntegrityError

    config = _config(probe_url)
    command.upgrade(config, "head")
    engine = create_engine(probe_url)
    episode_id = uuid.uuid4()
    artifact_insert = text(
        "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, "
        "bucket, object_key, sha256, input_hash, version, scene_id) VALUES "
        "(:id, :ep, :type, '1.0', 'b', 'k', :sha, :sha, :version, :scene)"
    )
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'assets_ready', 't')"),
            {"id": episode_id},
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, episode_id, type, status, attempts, max_attempts, scene_id) "
                "VALUES (:id, :ep, 'produce_scene_image', 'queued', 0, 1, 'sb1')"
            ),
            {"id": uuid.uuid4(), "ep": episode_id},
        )
        for scene in ("sb1", "sb2"):
            conn.execute(
                artifact_insert,
                {
                    "id": uuid.uuid4(),
                    "ep": episode_id,
                    "type": "scene_image",
                    "sha": "a" * 64,  # 同じ内容でもシーンが違えば別行
                    "version": 1,
                    "scene": scene,
                },
            )
        conn.execute(
            text(
                "INSERT INTO provider_reservations (id, episode_id, provider, idempotency_key, "
                "input_hash, round, status, scene_id, provider_job_ref, estimated_cost_usd) "
                "VALUES (:id, :ep, 'fal_video', :key, :key, 1, 'reserved', 'sb1', 'req', 0.1234)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "key": "k" * 64},
        )

    # 同じ scene キーに2本目の現行は作れない（Episode 単位の NULL キーも同様）
    def _artifact(artifact_type: str, sha: str, version: int, scene: str | None) -> dict:
        return {
            "id": uuid.uuid4(),
            "ep": episode_id,
            "type": artifact_type,
            "sha": sha,
            "version": version,
            "scene": scene,
        }

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(artifact_insert, _artifact("scene_image", "c" * 64, 2, "sb1"))
    with engine.begin() as conn:
        conn.execute(artifact_insert, _artifact("script", "b" * 64, 1, None))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(artifact_insert, _artifact("script", "c" * 64, 2, None))
    engine.dispose()

    # Phase 4 の行が残っていれば downgrade は失敗し、スキーマは head のまま
    # （downgrade は1トランザクション。head は 0006。ADR-0020）
    with pytest.raises(IntegrityError):
        command.downgrade(config, "0003")
    engine = create_engine(probe_url)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006"
        conn.execute(text("DELETE FROM episodes"))
    engine.dispose()

    command.downgrade(config, "0003")
    engine = create_engine(probe_url)
    columns = {c["name"] for c in inspect(engine).get_columns("provider_reservations")}
    assert {"scene_id", "provider_job_ref", "estimated_cost_usd"}.isdisjoint(columns)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'assets_ready', 't')"),
            {"id": uuid.uuid4()},
        )
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'storyboard_ready', 't')"),
            {"id": episode_id},
        )
        conn.execute(
            text(
                "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, "
                "bucket, object_key, sha256, input_hash, version) VALUES "
                "(:id, :ep, 'storyboard', '1.0', 'b', 'k', :sha, :sha, 1)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "sha": "d" * 64},
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:  # 0003 の現行一意性が戻っている
        conn.execute(
            text(
                "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, "
                "bucket, object_key, sha256, input_hash, version) VALUES "
                "(:id, :ep, 'storyboard', '1.0', 'b', 'k', :sha, :sha, 2)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "sha": "e" * 64},
        )
    engine.dispose()
    command.upgrade(config, "head")  # 再 upgrade が通る


def test_scene_scope_checks_on_postgres(probe_url) -> None:
    """0004: シーン単位の型は scene_id 必須、Episode 単位の型は scene_id NULL（ADR-0018）。"""
    import uuid

    from sqlalchemy.exc import IntegrityError

    config = _config(probe_url)
    command.upgrade(config, "head")
    engine = create_engine(probe_url)
    episode_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'in_progress', 't')"),
            {"id": episode_id},
        )

    artifact = text(
        "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, "
        "bucket, object_key, sha256, input_hash, version, scene_id) VALUES "
        "(:id, :ep, :type, '1.0', 'b', 'k', :sha, :sha, 1, :scene)"
    )
    job = text(
        "INSERT INTO jobs (id, episode_id, type, status, attempts, max_attempts, scene_id) "
        "VALUES (:id, :ep, :type, 'queued', 0, 1, :scene)"
    )
    reservation = text(
        "INSERT INTO provider_reservations (id, episode_id, provider, idempotency_key, "
        "input_hash, round, status, scene_id) VALUES (:id, :ep, :type, :key, :key, 1, "
        "'reserved', :scene)"
    )

    def _params(type_: str, scene: str | None) -> dict:
        return {
            "id": uuid.uuid4(),
            "ep": episode_id,
            "type": type_,
            "scene": scene,
            "sha": uuid.uuid4().hex + uuid.uuid4().hex,
            "key": uuid.uuid4().hex + uuid.uuid4().hex,
        }

    rejected = [
        (artifact, "scene_image", None),
        (artifact, "scene_video", None),
        (artifact, "scene_voice", None),
        (artifact, "script", "sb1"),
        (artifact, "storyboard", "sb1"),
        (artifact, "production_manifest", "sb1"),
        (job, "produce_scene_image", None),
        (job, "produce_scene_voice", None),
        (job, "produce_scene_video", None),
        (job, "write_script", "sb1"),
        (job, "assemble_production", "sb1"),
        (reservation, "fal_image", None),
        (reservation, "fal_video", None),
    ]
    for statement, type_, scene in rejected:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(statement, _params(type_, scene))

    accepted = [
        (artifact, "scene_image", "sb1"),
        (artifact, "scene_voice", "s1"),
        (artifact, "production_manifest", None),
        (job, "produce_scene_video", "sb1"),
        (job, "assemble_production", None),
        (reservation, "fal_video", "sb1"),
        (reservation, "codex_script", None),
    ]
    with engine.begin() as conn:
        for statement, type_, scene in accepted:
            conn.execute(statement, _params(type_, scene))
        conn.execute(text("DELETE FROM episodes"))
    engine.dispose()

    command.downgrade(config, "0003")
    engine = create_engine(probe_url)
    names = {
        c["name"]
        for table in ("artifact_metadata", "jobs", "provider_reservations")
        for c in inspect(engine).get_check_constraints(table)
    }
    assert not {n for n in names if n and "scene_scope" in n}
    engine.dispose()


def test_upload_vocabulary_and_result_ref_on_postgres(probe_url) -> None:
    """0006: upload の語彙と provider_result_ref（ADR-0020）。行が残れば downgrade は失敗する。"""
    import uuid

    from sqlalchemy.exc import IntegrityError

    config = _config(probe_url)
    command.upgrade(config, "head")
    engine = create_engine(probe_url)
    episode_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO episodes (id, status, topic) VALUES (:id, 'uploaded', 't')"),
            {"id": episode_id},
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, episode_id, type, status, attempts, max_attempts) "
                "VALUES (:id, :ep, 'upload_final_video', 'succeeded', 1, 3)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id},
        )
        conn.execute(
            text(
                "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, "
                "bucket, object_key, sha256, input_hash, version) VALUES "
                "(:id, :ep, 'upload_receipt', '1.0', 'b', 'k', :sha, :sha, 1)"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "sha": "a" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO provider_reservations (id, episode_id, provider, idempotency_key, "
                "input_hash, round, status, provider_result_ref) VALUES "
                "(:id, :ep, 'youtube_upload', :key, :key, 1, 'spent', 'abcdefghijk')"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "key": "b" * 64},
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:  # 同じ upload key は1行だけ
        conn.execute(
            text(
                "INSERT INTO provider_reservations (id, episode_id, provider, idempotency_key, "
                "input_hash, round, status) VALUES "
                "(:id, :ep, 'youtube_upload', :key, :key, 2, 'reserved')"
            ),
            {"id": uuid.uuid4(), "ep": episode_id, "key": "b" * 64},
        )
    engine.dispose()

    with pytest.raises(IntegrityError):
        command.downgrade(config, "0005")
    engine = create_engine(probe_url)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006"
        conn.execute(text("DELETE FROM episodes"))
    engine.dispose()

    command.downgrade(config, "0005")
    engine = create_engine(probe_url)
    columns = {c["name"] for c in inspect(engine).get_columns("provider_reservations")}
    assert "provider_result_ref" not in columns
    engine.dispose()
    command.upgrade(config, "head")
