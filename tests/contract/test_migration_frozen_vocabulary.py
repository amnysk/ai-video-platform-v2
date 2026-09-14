"""migration の downgrade が凍結した語彙が、その時点の enum 値と一致すること。

- 0003 → Phase 2（ced2aae）
- 0004 → Phase 3（c80a987）

以下は 0003 についての元の説明。

0003 の downgrade が凍結した Phase 2 語彙が ced2aae の enum 値と一致すること。

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


MIGRATION_0004 = REPO / "infrastructure/db/migrations/versions/0004_production_stage.py"

#: c80a987:contracts/states.py の EpisodeStatus（定義順）
C80A987_EPISODE_STATUSES = [
    *CED2AAE_EPISODE_STATUSES[:10],
    "storyboard_ready",
    "failed",
    "cancelled",
]
C80A987_JOB_TYPES = ["dummy", "write_script", "plan_storyboard"]
C80A987_ARTIFACT_TYPES = ["dummy", "script", "storyboard"]
C80A987_PROVIDER_CALLS = ["codex_script", "codex_storyboard"]


def _load_0004() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0004", MIGRATION_0004)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0004_downgrade_vocabulary_is_frozen_at_c80a987() -> None:
    migration = _load_0004()
    assert list(migration.PHASE3_EPISODE_STATUSES) == C80A987_EPISODE_STATUSES
    assert list(migration.PHASE3_JOB_TYPES) == C80A987_JOB_TYPES
    assert list(migration.PHASE3_ARTIFACT_TYPES) == C80A987_ARTIFACT_TYPES
    assert list(migration.PHASE3_PROVIDER_CALLS) == C80A987_PROVIDER_CALLS


def test_0004_downgrade_checks_cover_every_upgraded_check() -> None:
    migration = _load_0004()
    upgraded = {(t, n, c) for t, n, c, _ in migration._CHECKS}
    downgraded = {(t, n, c) for t, n, c, _ in migration._PHASE3_CHECKS}
    assert upgraded == downgraded


def test_0004_upgrade_adds_exactly_the_phase4_values() -> None:
    from contracts.states import ArtifactType, EpisodeStatus, JobType, ProviderCall

    migration = _load_0004()
    assert {s.value for s in EpisodeStatus} - set(migration.PHASE3_EPISODE_STATUSES) == {
        "assets_ready"
    }
    assert {t.value for t in JobType} - set(migration.PHASE3_JOB_TYPES) == {
        "produce_scene_image",
        "produce_scene_voice",
        "produce_scene_video",
        "assemble_production",
    }
    assert {t.value for t in ArtifactType} - set(migration.PHASE3_ARTIFACT_TYPES) == {
        "scene_image",
        "scene_voice",
        "scene_video",
        "production_manifest",
    }
    assert {p.value for p in ProviderCall} - set(migration.PHASE3_PROVIDER_CALLS) == {
        "fal_image",
        "fal_video",
    }


def test_0004_scene_scope_types_are_frozen() -> None:
    """0004 の scene_scope CHECK は literal で凍結し、今の語彙のシーン単位の型と一致する。"""
    from infrastructure.db import models

    migration = _load_0004()
    assert migration.SCENE_ARTIFACT_TYPES == ("scene_image", "scene_video", "scene_voice")
    assert migration.SCENE_JOB_TYPES == (
        "produce_scene_image",
        "produce_scene_video",
        "produce_scene_voice",
    )
    assert migration.SCENE_PROVIDER_CALLS == ("fal_image", "fal_video")
    assert {t.value for t in models.SCENE_ARTIFACT_TYPES} == set(migration.SCENE_ARTIFACT_TYPES)
    assert {t.value for t in models.SCENE_JOB_TYPES} == set(migration.SCENE_JOB_TYPES)
    assert {p.value for p in models.SCENE_PROVIDER_CALLS} == set(migration.SCENE_PROVIDER_CALLS)
