"""実行予算と並行数の既定値が契約の1箇所から来ること（API と workflow の食い違い防止）。"""

from __future__ import annotations

import contracts.production_activities as pa
from infrastructure.config import Settings
from workers.production.workflows import ProductionWorkflowInput


def test_settings_workflow_input_and_contract_defaults_agree() -> None:
    #: .env や環境変数に左右されないよう、宣言上の既定値を直接読む
    defaults = {name: f.default for name, f in Settings.model_fields.items()}
    wf = ProductionWorkflowInput(episode_id="e")
    expected = {
        "image_concurrency": pa.DEFAULT_IMAGE_CONCURRENCY,
        "voice_concurrency": pa.DEFAULT_VOICE_CONCURRENCY,
        "video_concurrency": pa.DEFAULT_VIDEO_CONCURRENCY,
    }
    for name, value in expected.items():
        assert defaults[name] == value
        assert getattr(wf, name) == value
    assert (
        defaults["production_image_max_rounds"]
        == wf.image_max_rounds
        == pa.DEFAULT_IMAGE_MAX_ROUNDS
    )
    assert (
        defaults["production_video_max_rounds"]
        == wf.video_max_rounds
        == pa.DEFAULT_VIDEO_MAX_ROUNDS
    )
    assert (
        defaults["production_await_reexecutions"]
        == wf.await_reexecutions
        == pa.DEFAULT_AWAIT_REEXECUTIONS
    )


def test_scene_recovery_limits_have_one_default_and_reach_the_workflow_and_schedule() -> None:
    """ADR-0035 (8): 内容拒否からの復旧の上限は設定値。既定値は contracts の1箇所。

    workflow の中では Settings を読まない（非決定的）。1実行で planner を呼ぶシーンの上限は
    workflow の入力（``max_scene_alternatives_per_scene``）で渡し、Schedule・API・pipeline の
    どこから起動しても同じ設定値が届く。DB 側の判定（Activity）も同じ設定値を使う。
    """
    from contracts.pipeline import PipelineOptions, PipelineStage, ProductionParameters
    from infrastructure.temporal.schedules import daily_episode_input_from_settings
    from workers.pipeline.workflows import _stage_request

    defaults = {name: f.default for name, f in Settings.model_fields.items()}
    assert (
        defaults["production_max_scene_alternatives_per_scene"]
        == ProductionWorkflowInput(episode_id="e").max_scene_alternatives_per_scene
        == ProductionParameters().max_scene_alternatives_per_scene
        == pa.MAX_SCENE_ALTERNATIVES_PER_SCENE
    )
    assert (
        defaults["production_max_scene_alternatives_per_episode"]
        == pa.MAX_SCENE_ALTERNATIVES_PER_EPISODE
    )
    assert defaults["production_max_recovery_cost_usd"] == pa.MAX_RECOVERY_COST_USD_PER_EPISODE

    settings = Settings(production_max_scene_alternatives_per_scene=1)
    wf_input = daily_episode_input_from_settings(settings)
    assert wf_input.options.production.max_scene_alternatives_per_scene == 1
    _name, _id, arg = _stage_request(
        PipelineStage.PRODUCTION, "e", PipelineOptions(production=wf_input.options.production)
    )
    assert arg["max_scene_alternatives_per_scene"] == 1
