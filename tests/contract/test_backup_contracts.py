"""backup の識別子・manifest が compose.yaml・release preflight と食い違わないこと（ADR-0051）。"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timedelta
from typing import Any

import contracts.backup as cb
import contracts.release as cr
from domain import release_preflight as rp
from infrastructure.workdir import MINIO_DATA_DIR
from tests.unit.backup.fakes import make_backup

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = (REPO / "compose.yaml").read_text(encoding="utf-8")


def test_component_names_are_the_ones_release_preflight_reads() -> None:
    assert set(rp.BACKUP_COMPONENTS) == {
        cb.COMPONENT_DB_DUMP,
        cb.COMPONENT_MINIO,
        cb.COMPONENT_TEMPORAL,
    }


def test_production_identifiers_match_compose() -> None:
    assert re.search(rf"^name: {cb.PRODUCTION_COMPOSE_PROJECT}$", COMPOSE, re.MULTILINE)
    assert f'ports: ["{cb.PRODUCTION_POSTGRES_PORT}:5432"]' in COMPOSE
    assert 'ports: ["9000:9000", "9001:9001"]' in COMPOSE
    assert tuple(cb.PRODUCTION_MINIO_PORTS) == (9000, 9001)
    assert f'ports: ["{cb.PRODUCTION_TEMPORAL_PORT}:7233"]' in COMPOSE
    assert f"POSTGRES_DB: ${{POSTGRES_DB:-{cb.APP_DATABASE}}}" in COMPOSE
    assert f"{MINIO_DATA_DIR}:/data" in COMPOSE  # 保護するパスの宣言元は infrastructure/workdir.py
    # コンテナ名は project 名 + service 名 + 連番（compose の既定）
    project = cb.PRODUCTION_COMPOSE_PROJECT
    assert f"{project}-postgres-1" == cb.PRODUCTION_POSTGRES_CONTAINER
    assert f"{project}-minio-1" == cb.PRODUCTION_MINIO_CONTAINER
    # Temporal は PG を使う（バックアップ対象が PG に含まれる根拠）
    assert "DB: postgres12" in COMPOSE and "POSTGRES_SEEDS: postgres" in COMPOSE


def test_the_default_bucket_is_what_the_application_writes() -> None:
    assert f"MINIO_BUCKET: ${{MINIO_BUCKET:-{cb.PRODUCTION_BUCKETS[0]}}}" in COMPOSE


def test_test_databases_are_not_backed_up_and_restore_names_cannot_be_production() -> None:
    assert "avp_test" not in cb.BACKUP_DATABASES and "postgres" not in cb.BACKUP_DATABASES
    assert not any(cr.TEST_DB_SUFFIX in d for d in cb.BACKUP_DATABASES)
    for db in cb.BACKUP_DATABASES:
        assert (cb.RESTORE_DB_PREFIX + db) not in cb.BACKUP_DATABASES
    assert not any(b.startswith(cb.RESTORE_BUCKET_PREFIX) for b in cb.PRODUCTION_BUCKETS)
    assert cr.STAGING_DB_NAME not in cb.BACKUP_DATABASES
    assert cb.RESTORE_POSTGRES_IMAGE in COMPOSE  # 本番と同じ major の PG に戻す


class _Collectors:
    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, path: str) -> str | None:
        return self._text


def _preflight_backup_status(manifest: dict[str, Any], now: datetime) -> rp.Status:
    req = rp.Request(target_sha="a" * 40, backup_manifest="manifest.json")
    result = rp._check_backup(req, _Collectors(json.dumps(manifest)), now)  # type: ignore[arg-type]
    return result.status


def test_a_real_manifest_passes_the_release_preflight_backup_check(tmp_path: pathlib.Path) -> None:
    h = make_backup(tmp_path)
    manifest = h.service.run()
    done = datetime.fromisoformat(manifest["finished_at"])
    assert _preflight_backup_status(manifest, done + timedelta(hours=1)) is rp.Status.PASS
    assert _preflight_backup_status(manifest, done + timedelta(hours=30)) is rp.Status.BLOCKED
    assert _preflight_backup_status({"schema_version": 1}, done) is rp.Status.UNKNOWN
    # 時刻は全て TZ 付き（naive は preflight が「確認できない」とする）
    for comp in manifest["components"].values():
        assert datetime.fromisoformat(comp["created_at"]).tzinfo is not None
