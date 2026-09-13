"""0003 の downgrade が凍結した Phase 2 語彙が ced2aae の enum 値と一致すること。

期待値は ``git show ced2aae:contracts/states.py`` から書き写した literal。現在の
``contracts.states`` から導出しない（enum が将来変わっても履歴の downgrade 先は変わらない）。
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "infrastructure/db/migrations/versions/0003_storyboard_stage.py"

#: ced2aae:contracts/states.py の EpisodeStatus（定義順）
CED2AAE_EPISODE_STATUSES = [
    "planned",
    "in_progress",
    "needs_work",
    "blocked",
    "ready_for_review",
    "approved",
    "uploaded",
    "analyzed",
    "completed",
    "script_ready",
    "failed",
    "cancelled",
]
CED2AAE_JOB_TYPES = ["dummy", "write_script"]
CED2AAE_ARTIFACT_TYPES = ["dummy", "script"]
CED2AAE_PROVIDER_CALLS = ["codex_script"]


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downgrade_vocabulary_is_frozen_at_ced2aae() -> None:
    migration = _load_migration()
    assert list(migration.PHASE2_EPISODE_STATUSES) == CED2AAE_EPISODE_STATUSES
    assert list(migration.PHASE2_JOB_TYPES) == CED2AAE_JOB_TYPES
    assert list(migration.PHASE2_ARTIFACT_TYPES) == CED2AAE_ARTIFACT_TYPES
    assert list(migration.PHASE2_PROVIDER_CALLS) == CED2AAE_PROVIDER_CALLS


def test_downgrade_checks_cover_every_upgraded_check() -> None:
    migration = _load_migration()
    upgraded = {(t, n, c) for t, n, c, _ in migration._CHECKS}
    downgraded = {(t, n, c) for t, n, c, _ in migration._PHASE2_CHECKS}
    assert upgraded == downgraded
