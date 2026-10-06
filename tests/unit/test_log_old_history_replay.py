"""ログ導入前（01eb0ee）の workflow の履歴が、ログ発行を足した今のコードで replay できる（INV-40）。

稼働中の workflow は導入前のコードで始まった履歴を持ったまま、新しい worker に拾われる。
ログ発行がコマンドを足したり分岐を変えたりしていれば、ここで非決定（NondeterminismError）になる。

履歴は ``tests/unit/fixtures/pre_logging/`` に、01eb0ee を一時 worktree に checkout し、既存の
workflow テストを ``tests/support/history_capture_plugin.py`` つきで走らせて採った（手順は
docs/testing/logging-rationale.md）。Production は代替映像案（scene recovery の patched 分岐）の
成功・上限の経路を含む。理由は docs/testing/logging-rationale.md（レビュー I-3）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from tests.support.log_capture import capture_json
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow
from workers.production.workflows import ProductionWorkflow
from workers.render.workflows import RenderWorkflow
from workers.storyboard.workflows import StoryboardWorkflow
from workers.upload.workflows import UploadWorkflow

FIXTURES = Path(__file__).parent / "fixtures" / "pre_logging"
WORKFLOWS = [
    DailyEpisodeWorkflow,
    EpisodePipelineWorkflow,
    ProductionWorkflow,
    RenderWorkflow,
    StoryboardWorkflow,
    UploadWorkflow,
]
#: 少なくともこれだけの経路を持つ（fixture を消して検査が空振りしないように）
REQUIRED = {
    "production_scene_alternative_rebuilt",
    "production_scene_alternative_limit",
    "production_voice_gate_failed",
    "production_succeeded",
    "render_succeeded",
    "render_blocked",
    "upload_succeeded",
    "upload_blocked",
    "storyboard_succeeded",
    "storyboard_blocked",
    "pipeline_completed",
    "pipeline_resume_mid",
    "pipeline_upload_gate_refused",
    "daily_started",
}


def _histories() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_the_old_histories_cover_every_stage_workflow() -> None:
    assert {p.stem for p in _histories()} >= REQUIRED


@pytest.mark.parametrize("path", _histories(), ids=lambda p: p.stem)
async def test_old_history_replays_deterministically_and_emits_nothing(path: Path) -> None:
    history = WorkflowHistory.from_json(path.stem, path.read_text(encoding="utf-8"))
    with capture_json() as logs:
        await Replayer(workflows=WORKFLOWS).replay_workflow(history)
    # replay 中の発行は SDK が抑止する（業務イベントを重複発行しない）
    assert [e for e in logs.events() if e["logger"] == "temporalio.workflow"] == []
