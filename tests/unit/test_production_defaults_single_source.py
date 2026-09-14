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
