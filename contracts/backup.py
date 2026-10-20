"""バックアップの識別子と manifest の語彙（唯一の宣言元。AGENTS.md §8 / ADR-0051）。

backup の実行側（``infrastructure/backup``）・判定側（``domain/backup``）・テストが同じ値を使う。
本番の識別子は ``compose.yaml`` の値。一致は ``tests/contract/test_backup_contracts.py``。
"""

from __future__ import annotations

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
#: release preflight（``domain/release_preflight.BACKUP_COMPONENTS``）が読む component 名。
#: 一致は tests/contract/test_backup_contracts.py が検査する
COMPONENT_DB_DUMP = "db_dump"
COMPONENT_MINIO = "minio"
COMPONENT_TEMPORAL = "temporal"

#: ``backup_id`` の書式（UTC。ディレクトリ名と同じ）。prune はこの形のディレクトリだけを触る
BACKUP_ID_FORMAT = "%Y%m%dT%H%M%SZ"
BACKUP_ID_PATTERN = r"^\d{8}T\d{6}Z$"

STATUS_COMPLETE = "complete"

# --- 本番の識別子（compose.yaml）-----------------------------------------------------
PRODUCTION_COMPOSE_PROJECT = "avp2"
PRODUCTION_POSTGRES_CONTAINER = "avp2-postgres-1"
PRODUCTION_MINIO_CONTAINER = "avp2-minio-1"
PRODUCTION_POSTGRES_PORT = 5432
#: MinIO の S3 / console
PRODUCTION_MINIO_PORTS = (9000, 9001)
PRODUCTION_TEMPORAL_PORT = 7233
#: compose ネットワーク内の本番 service 名。loopback は本番ポートと組のときだけ本番（port で判定）
PRODUCTION_SERVICE_HOSTS = ("postgres", "minio", "temporal")
#: アプリ DB（``avp``）と Temporal の 2 つ。postgres / template* / ``avp_test`` は対象外
APP_DATABASE = "avp"
TEMPORAL_DATABASES = ("temporal", "temporal_visibility")
BACKUP_DATABASES = (APP_DATABASE, *TEMPORAL_DATABASES)
#: 本番 bucket。``avp-itest`` 系・``artifacts-test`` 等のテスト bucket は既定では対象外
PRODUCTION_BUCKETS = ("artifacts",)
#: DB が bucket / object_key / sha256 を持つ表（PG↔MinIO の参照整合性を検査する）
ARTIFACT_REFERENCE_TABLES = ("artifact_metadata", "research_artifacts")
#: 本番が実データを置く host path。復旧先・backup 先に使えない（restore の拒否）
PRODUCTION_DATA_PATHS = ("/mnt/minio-hdd/minio-data", "/mnt/minio-hdd/ai-video-work")

# --- 復旧先（隔離）-----------------------------------------------------------------
RESTORE_CONTAINER_PREFIX = "avp-restore-"
RESTORE_DB_PREFIX = "restore_"
RESTORE_BUCKET_PREFIX = "restore-"
RESTORE_POSTGRES_IMAGE = "postgres:17-alpine"

# --- 保持（既定）-------------------------------------------------------------------
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 6

# --- 環境変数（既定は全て未設定 = 外部送信なし・通知なし）---------------------------------
ENV_DB_ROOT = "AVP_BACKUP_DB_ROOT"
ENV_OBJECT_ROOT = "AVP_BACKUP_OBJECT_ROOT"
ENV_NOTIFY_CMD = "AVP_BACKUP_NOTIFY_CMD"
ENV_NOTIFY_WEBHOOK = "AVP_BACKUP_NOTIFY_WEBHOOK"
ENV_OFFSITE_CMD = "AVP_BACKUP_OFFSITE_CMD"

EXIT_OK = 0
EXIT_FAILED = 1
#: 使い方の誤り・拒否（復旧先が本番など）
EXIT_REFUSED = 2
