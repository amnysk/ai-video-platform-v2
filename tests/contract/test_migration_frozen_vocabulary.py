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
    """0004 が足した値 = Phase 4（dad86f5）の凍結語彙 - Phase 3 の凍結語彙。

    Phase 5（0005）で enum が増えたので、今の enum ではなく 0005 の downgrade が凍結した
    Phase 4 の値と比べる（仕様変更: ADR-0019。0004 の意味は変わっていない）。
    """
    migration = _load_0004()
    assert set(DAD86F5_EPISODE_STATUSES) - set(migration.PHASE3_EPISODE_STATUSES) == {
        "assets_ready"
    }
    assert set(DAD86F5_JOB_TYPES) - set(migration.PHASE3_JOB_TYPES) == {
        "produce_scene_image",
        "produce_scene_voice",
        "produce_scene_video",
        "assemble_production",
    }
    assert set(DAD86F5_ARTIFACT_TYPES) - set(migration.PHASE3_ARTIFACT_TYPES) == {
        "scene_image",
        "scene_voice",
        "scene_video",
        "production_manifest",
    }
    # Phase 6（0006）で provider が増えたので、0006 が凍結した Phase 5 の値と比べる（ADR-0020）
    assert set(A51BC13_PROVIDER_CALLS) - set(migration.PHASE3_PROVIDER_CALLS) == {
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


MIGRATION_0005 = REPO / "infrastructure/db/migrations/versions/0005_render_stage.py"

#: dad86f5:contracts/states.py の EpisodeStatus（定義順）
DAD86F5_EPISODE_STATUSES = [
    *C80A987_EPISODE_STATUSES[:11],
    "assets_ready",
    "failed",
    "cancelled",
]
DAD86F5_JOB_TYPES = [
    *C80A987_JOB_TYPES,
    "produce_scene_image",
    "produce_scene_voice",
    "produce_scene_video",
    "assemble_production",
]
DAD86F5_ARTIFACT_TYPES = [
    *C80A987_ARTIFACT_TYPES,
    "scene_image",
    "scene_voice",
    "scene_video",
    "production_manifest",
]


def _load_0005() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0005", MIGRATION_0005)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0005_downgrade_vocabulary_is_frozen_at_dad86f5() -> None:
    migration = _load_0005()
    assert list(migration.PHASE4_EPISODE_STATUSES) == DAD86F5_EPISODE_STATUSES
    assert list(migration.PHASE4_JOB_TYPES) == DAD86F5_JOB_TYPES
    assert list(migration.PHASE4_ARTIFACT_TYPES) == DAD86F5_ARTIFACT_TYPES


def test_0005_downgrade_checks_cover_every_upgraded_check() -> None:
    migration = _load_0005()
    upgraded = {(t, n, c) for t, n, c, _ in migration._CHECKS}
    downgraded = {(t, n, c) for t, n, c, _ in migration._PHASE4_CHECKS}
    assert upgraded == downgraded


def test_0005_upgrade_adds_exactly_the_phase5_values() -> None:
    """Phase 6（0006）で job / artifact が増えたので、0006 が凍結した Phase 5 の値と比べる。"""
    from contracts.states import EpisodeStatus

    migration = _load_0005()
    assert {s.value for s in EpisodeStatus} - set(migration.PHASE4_EPISODE_STATUSES) == {
        "render_ready"
    }
    assert set(A51BC13_JOB_TYPES) - set(migration.PHASE4_JOB_TYPES) == {"render_final_video"}
    assert set(A51BC13_ARTIFACT_TYPES) - set(migration.PHASE4_ARTIFACT_TYPES) == {"final_video"}


def test_final_video_is_episode_scoped() -> None:
    """final_video / render_final_video はシーン単位ではない（scene_id は NULL）。"""
    from contracts.states import ArtifactType, JobType
    from infrastructure.db import models

    assert ArtifactType.FINAL_VIDEO not in models.SCENE_ARTIFACT_TYPES
    assert JobType.RENDER_FINAL_VIDEO not in models.SCENE_JOB_TYPES


MIGRATION_0006 = REPO / "infrastructure/db/migrations/versions/0006_upload_stage.py"

#: a51bc13:contracts/states.py の JobType / ArtifactType / ProviderCall（定義順）
A51BC13_JOB_TYPES = [*DAD86F5_JOB_TYPES, "render_final_video"]
A51BC13_ARTIFACT_TYPES = [*DAD86F5_ARTIFACT_TYPES, "final_video"]
A51BC13_PROVIDER_CALLS = ["codex_script", "codex_storyboard", "fal_image", "fal_video"]


def _load_0006() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION_0006)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0006_downgrade_vocabulary_is_frozen_at_a51bc13() -> None:
    migration = _load_0006()
    assert list(migration.PHASE5_JOB_TYPES) == A51BC13_JOB_TYPES
    assert list(migration.PHASE5_ARTIFACT_TYPES) == A51BC13_ARTIFACT_TYPES
    assert list(migration.PHASE5_PROVIDER_CALLS) == A51BC13_PROVIDER_CALLS


def test_0006_downgrade_checks_cover_every_upgraded_check() -> None:
    migration = _load_0006()
    upgraded = {(t, n, c) for t, n, c, _ in migration._CHECKS}
    downgraded = {(t, n, c) for t, n, c, _ in migration._PHASE5_CHECKS}
    assert upgraded == downgraded


def test_0006_upgrade_adds_exactly_the_phase6_values() -> None:
    """Episode 状態は増えない（既存の uploaded を使う。ADR-0020）。"""
    from contracts.states import ArtifactType, EpisodeStatus, JobType, ProviderCall

    migration = _load_0006()
    assert {s.value for s in EpisodeStatus} == set(DAD86F5_EPISODE_STATUSES) | {"render_ready"}
    assert {t.value for t in JobType} - set(migration.PHASE5_JOB_TYPES) == {"upload_final_video"}
    assert {t.value for t in ArtifactType} - set(migration.PHASE5_ARTIFACT_TYPES) == {
        "upload_receipt"
    }
    assert {p.value for p in ProviderCall} - set(migration.PHASE5_PROVIDER_CALLS) == {
        "youtube_upload"
    }


def test_upload_vocabulary_is_episode_scoped() -> None:
    """upload の語彙はシーン単位ではない（scene_id は NULL）。"""
    from contracts.states import ArtifactType, JobType, ProviderCall
    from infrastructure.db import models

    assert ArtifactType.UPLOAD_RECEIPT not in models.SCENE_ARTIFACT_TYPES
    assert JobType.UPLOAD_FINAL_VIDEO not in models.SCENE_JOB_TYPES
    assert ProviderCall.YOUTUBE_UPLOAD not in models.SCENE_PROVIDER_CALLS
