"""本番で記録した DailyWatchdogWorkflow の履歴を、現在の workflow コードで replay する（INV-40）。

`tests/unit/fixtures/daily_watchdog_prod_2026_10_24_history.json` は 2026-10-24T14:35Z の
本番の実行（コード 80775c0、Completed）を `fetch_history` で読み出したもの。入力・結果は
運用の設定値と件数だけで、秘密・URL・メディアを含まない（取得時に確認）。

watchdog workflow は毎時走り、ADR-0054（日次 workflow 失敗の検知）が workflow のコードを触る。
デプロイ時に本番で進行中の watchdog の履歴は新しいコードで replay されるので、Activity の
順序・数が変わって非決定になれば、ここで落ちる。履歴の入力に無い新しいフィールドは
dataclass の既定値で埋まる（replay は入力の型を検査しない）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from workers.pipeline.watchdog import DailyWatchdogWorkflow

FIXTURE = Path(__file__).parent / "fixtures" / "daily_watchdog_prod_2026_10_24_history.json"


@pytest.mark.asyncio
async def test_recorded_production_watchdog_history_replays_without_nondeterminism() -> None:
    history = WorkflowHistory.from_json("daily-watchdog-recorded", FIXTURE.read_text())
    await Replayer(workflows=[DailyWatchdogWorkflow]).replay_workflow(history)


def test_the_fixture_is_a_real_completed_run_with_the_watchdog_activity() -> None:
    """fixture が空・別 workflow・失敗した実行に差し替わって、replay が空振りするのを防ぐ。"""
    events = json.loads(FIXTURE.read_text())["events"]
    types = [e["eventType"] for e in events]
    assert types[0] == "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"
    assert types[-1] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"
    assert "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED" in types
    assert "EVENT_TYPE_ACTIVITY_TASK_COMPLETED" in types
    started = events[0]["workflowExecutionStartedEventAttributes"]
    assert started["workflowType"]["name"] == "DailyWatchdogWorkflow"


@pytest.mark.asyncio
async def test_a_changed_activity_order_would_be_caught_by_this_replay() -> None:
    """replay が本当に履歴と突き合わせていること: Activity 名を書き換えた履歴は非決定で落ちる。"""
    raw = FIXTURE.read_text()
    activity_type = _first_activity_type(raw)
    tampered = raw.replace(f'"name": "{activity_type}"', '"name": "some_other_activity"')
    assert tampered != raw
    history = WorkflowHistory.from_json("daily-watchdog-tampered", tampered)
    with pytest.raises(Exception, match=r"[Nn]ondeterminism|some_other_activity"):
        await Replayer(workflows=[DailyWatchdogWorkflow]).replay_workflow(history)


def _first_activity_type(raw: str) -> str:
    for event in json.loads(raw)["events"]:
        attrs = event.get("activityTaskScheduledEventAttributes")
        if attrs:
            return attrs["activityType"]["name"]
    raise AssertionError("no activity in fixture")
