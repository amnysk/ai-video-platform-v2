#!/usr/bin/env python
"""復旧できるバックアップ（ADR-0051 / INV-66）。

    python scripts/backup.py run             # PG 3 DB → MinIO。manifest は restore_verified=false
    python scripts/backup.py restore-drill --backup-id latest   # 使い捨ての復旧先で検証
    python scripts/backup.py prune [--apply]  # 検証済みの古い集合だけを消す（既定 dry-run）
    python scripts/backup.py verify --backup-id ID   # 全ファイルの再ハッシュ
    python scripts/backup.py status

保存先は AVP_BACKUP_DB_ROOT / AVP_BACKUP_OBJECT_ROOT（既定なし）。外部送信・通知は既定オフ。
ロジック: infrastructure/backup、判定: domain/backup、手順: docs/operations/backup-restore.md
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from infrastructure.backup.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
