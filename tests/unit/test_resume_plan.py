"""domain/pipeline/resume_plan.py の単体テスト（ADR-0032）。

純粋関数のみを対象にする。DB・Temporal・HTTPには一切触れない。
"""

from __future__ import annotations

from contracts.pipeline import (
    PipelineStage,
    production_workflow_id,
    render_workflow_id,
    upload_workflow_id,
)
from contracts.states import EpisodeStatus, ProviderCall, ReservationStatus
from domain.pipeline.resume_plan import (
    STAGE_PROVIDERS,
    ReservationSummary,
    build_resume_plan,
    determine_target_stage,
)

EP = "11111111-1111-1111-1111-111111111111"


def _reservation(
    *,
    status: ReservationStatus = ReservationStatus.RESERVED,
    raw_output_key: str | None = None,
    id: str = "r1",  # noqa: A002
    provider: ProviderCall = ProviderCall.FAL_IMAGE,
) -> ReservationSummary:
    return ReservationSummary(
        id=id, provider=provider, status=status, raw_output_key=raw_output_key
    )


# --------------------------------------------------------------------- determine_target_stage


def test_script_ready_resumes_at_storyboard() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.SCRIPT_READY, episode_id=EP, owner_workflow_id=None
    )
    assert stage is PipelineStage.STORYBOARD
    assert reason is None


def test_storyboard_ready_resumes_at_production() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.STORYBOARD_READY, episode_id=EP, owner_workflow_id=None
    )
    assert stage is PipelineStage.PRODUCTION
    assert reason is None


def test_assets_ready_resumes_at_render() -> None:
    stage, _ = determine_target_stage(
        status=EpisodeStatus.ASSETS_READY, episode_id=EP, owner_workflow_id=None
    )
    assert stage is PipelineStage.RENDER


def test_render_ready_resumes_at_upload() -> None:
    stage, _ = determine_target_stage(
        status=EpisodeStatus.RENDER_READY, episode_id=EP, owner_workflow_id=None
    )
    assert stage is PipelineStage.UPLOAD


def test_uploaded_is_not_resumable_nothing_left_to_do() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.UPLOADED, episode_id=EP, owner_workflow_id=None
    )
    assert stage is None
    assert reason is not None and "already" in reason.lower()


def test_in_progress_is_not_a_resumable_status() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.IN_PROGRESS, episode_id=EP, owner_workflow_id=None
    )
    assert stage is None
    assert reason is not None


def test_planned_is_not_a_resumable_status() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.PLANNED, episode_id=EP, owner_workflow_id=None
    )
    assert stage is None
    assert reason is not None


def test_blocked_owned_by_production_resumes_at_production() -> None:
    owner = f"{production_workflow_id(EP)}:run-1"
    stage, reason = determine_target_stage(
        status=EpisodeStatus.BLOCKED, episode_id=EP, owner_workflow_id=owner
    )
    assert stage is PipelineStage.PRODUCTION
    assert reason is None


def test_needs_work_owned_by_render_resumes_at_render() -> None:
    owner = f"{render_workflow_id(EP)}:run-2"
    stage, reason = determine_target_stage(
        status=EpisodeStatus.NEEDS_WORK, episode_id=EP, owner_workflow_id=owner
    )
    assert stage is PipelineStage.RENDER
    assert reason is None


def test_blocked_owned_by_upload_resumes_at_upload() -> None:
    owner = f"{upload_workflow_id(EP)}:run-3"
    stage, reason = determine_target_stage(
        status=EpisodeStatus.BLOCKED, episode_id=EP, owner_workflow_id=owner
    )
    assert stage is PipelineStage.UPLOAD
    assert reason is None


def test_blocked_with_owner_from_a_different_episode_is_not_resumable() -> None:
    other_episode_owner = f"{production_workflow_id('other-episode')}:run-1"
    stage, reason = determine_target_stage(
        status=EpisodeStatus.BLOCKED, episode_id=EP, owner_workflow_id=other_episode_owner
    )
    assert stage is None
    assert reason is not None and "does not match" in reason


def test_blocked_with_unknown_owner_is_not_resumable() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.BLOCKED,
        episode_id=EP,
        owner_workflow_id=f"episode-{EP}-storyboard:run-1",
    )
    assert stage is None
    assert reason is not None and "does not match" in reason


def test_blocked_with_no_owner_recorded_is_not_resumable() -> None:
    stage, reason = determine_target_stage(
        status=EpisodeStatus.BLOCKED, episode_id=EP, owner_workflow_id=None
    )
    assert stage is None
    assert reason is not None


# ------------------------------------------------------------------------- build_resume_plan


def test_resumable_plan_lists_all_remaining_stages() -> None:
    plan = build_resume_plan(
        episode_id=EP, status=EpisodeStatus.STORYBOARD_READY, owner_workflow_id=None
    )
    assert plan.resumable is True
    assert plan.target_stage == PipelineStage.PRODUCTION.value
    assert plan.stages_to_run == ("production", "render", "upload")
    assert plan.unresolved_blockers == ()
    assert plan.reason is None


def test_unreconciled_reservation_blocks_resume() -> None:
    plan = build_resume_plan(
        episode_id=EP,
        status=EpisodeStatus.STORYBOARD_READY,
        owner_workflow_id=None,
        unreconciled_reservations=[_reservation()],
    )
    assert plan.resumable is False
    assert plan.unreconciled_reservations
    assert plan.reason is not None


def test_reconciled_reservations_do_not_block() -> None:
    """status=spent（成否確定済み）は照合の対象ではない。

    呼び出し側は unreconciled だけを渡す想定だが、念のため関数自身も RESERVED 以外は
    無視することを検査する。
    """
    plan = build_resume_plan(
        episode_id=EP,
        status=EpisodeStatus.STORYBOARD_READY,
        owner_workflow_id=None,
        unreconciled_reservations=[_reservation(status=ReservationStatus.SPENT)],
    )
    assert plan.resumable is True
    assert plan.unreconciled_reservations == ()


def test_no_reservations_do_not_block() -> None:
    plan = build_resume_plan(
        episode_id=EP,
        status=EpisodeStatus.STORYBOARD_READY,
        owner_workflow_id=None,
        unreconciled_reservations=[],
    )
    assert plan.resumable is True


def test_ownership_mismatch_produces_an_unresolved_blocker() -> None:
    plan = build_resume_plan(
        episode_id=EP,
        status=EpisodeStatus.BLOCKED,
        owner_workflow_id=f"episode-{EP}-storyboard:run-1",
    )
    assert plan.resumable is False
    assert plan.target_stage is None
    assert plan.unresolved_blockers


def test_already_uploaded_is_not_resumable() -> None:
    plan = build_resume_plan(episode_id=EP, status=EpisodeStatus.UPLOADED, owner_workflow_id=None)
    assert plan.resumable is False
    assert plan.target_stage is None
    assert plan.stages_to_run == ()


def test_target_render_lists_only_render_and_upload() -> None:
    plan = build_resume_plan(
        episode_id=EP, status=EpisodeStatus.ASSETS_READY, owner_workflow_id=None
    )
    assert plan.stages_to_run == ("render", "upload")


# --------------------------------------------------------------------- possible_new_charges


def test_possible_new_charges_excludes_render_which_has_no_provider() -> None:
    """render は STAGE_PROVIDERS が空（外部呼び出しが無いローカル描画）なので開示に出ない。

    upload は YOUTUBE_UPLOAD provider を持つので出る（投稿は課金は無いが台帳に載る外部副作用。
    ここでは「provider が関わる」という開示の定義なので含めてよい — ADR-0032 のスコープ
    決定を参照）。
    """
    plan = build_resume_plan(
        episode_id=EP, status=EpisodeStatus.ASSETS_READY, owner_workflow_id=None
    )
    assert plan.stages_to_run == ("render", "upload")
    assert plan.possible_new_charges == ("upload",)


def test_possible_new_charges_lists_production_and_upload_when_resuming_from_storyboard_ready() -> (
    None
):
    plan = build_resume_plan(
        episode_id=EP, status=EpisodeStatus.STORYBOARD_READY, owner_workflow_id=None
    )
    assert plan.stages_to_run == ("production", "render", "upload")
    assert plan.possible_new_charges == ("production", "upload")


def test_possible_new_charges_is_a_disclosure_not_a_guarantee_of_a_charge() -> None:
    """input_hash が不変ならこの工程は再課金しない（INV-17）。それでも一覧には出る:

    「課金が起きうる工程」の開示であって「課金する」という予告ではないため。実際の
    課金判定は Activity の input_hash 比較に委ねる（このテストは開示の存在だけを検査し、
    実際に課金が起きないことは production の Activity 側の既存テストが担う）。
    """
    plan = build_resume_plan(
        episode_id=EP, status=EpisodeStatus.STORYBOARD_READY, owner_workflow_id=None
    )
    assert "production" in plan.possible_new_charges


def test_possible_new_charges_is_empty_when_not_resumable_and_no_target_stage() -> None:
    plan = build_resume_plan(episode_id=EP, status=EpisodeStatus.UPLOADED, owner_workflow_id=None)
    assert plan.resumable is False
    assert plan.possible_new_charges == ()


# --------------------------------------------------------------------------- STAGE_PROVIDERS


def test_stage_providers_single_source_mapping() -> None:
    assert STAGE_PROVIDERS[PipelineStage.PRODUCTION] == (
        ProviderCall.FAL_IMAGE,
        ProviderCall.FAL_VIDEO,
    )
    assert STAGE_PROVIDERS[PipelineStage.STORYBOARD] == (ProviderCall.CODEX_STORYBOARD,)
    assert STAGE_PROVIDERS[PipelineStage.UPLOAD] == (ProviderCall.YOUTUBE_UPLOAD,)
    assert STAGE_PROVIDERS[PipelineStage.RENDER] == ()
