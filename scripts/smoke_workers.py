#!/usr/bin/env python
"""scripts/smoke-workers.sh の補助（host の venv から localhost の Temporal を叩く）。

**無料の操作だけ**を持つ。有料・外部呼び出しの queue に workflow を投げる手段は置かない。

    smoke_workers.py pollers --queue upload      # poller identity を列挙
    smoke_workers.py daily-paused                # paused 前提で DailyEpisodeWorkflow を1回
    smoke_workers.py gate-probe                  # pipeline-worker の check_paused / upload_gate

``daily-paused`` は DB スイッチ paused が on であることを呼び出し側が保証する
（off なら子 workflow を起動しうるので、ここでも事前に確かめて拒否する）。
``gate-probe`` は一意な queue の使い捨て workflow から、
Activity だけを queue ``pipeline`` に投げる。
存在しない episode id を使うので、upload_gate は状態を読むだけで何も書かない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from temporalio import workflow  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.common import RetryPolicy  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from contracts.operations import OperationalSwitch  # noqa: E402
from contracts.pipeline import (  # noqa: E402
    DAILY_EPISODE_WORKFLOW,
    PIPELINE_CHECK_PAUSED,
    PIPELINE_TASK_QUEUE,
    PIPELINE_UPLOAD_GATE,
    CheckPausedRequest,
    CheckPausedResult,
    DailyEpisodeInput,
    DailyEpisodeResult,
    UploadGateRequest,
    UploadGateResult,
)
from infrastructure.config import Settings  # noqa: E402

# 存在しない episode（uuid 形式でないと repository が弾く）
PROBE_EPISODE_ID = str(uuid.UUID(int=0))


async def _client(settings: Settings) -> Client:
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


async def cmd_pollers(settings: Settings, queues: list[str]) -> int:
    from infrastructure.temporal.poller_check import temporal_describe_factory

    describe = temporal_describe_factory(settings.temporal_address, settings.temporal_namespace)
    out = {q: sorted({identity for identity, _ in await describe(q)}) for q in queues}
    print(json.dumps(out))
    return 0


async def _db_switch_on(settings: Settings, switch: OperationalSwitch) -> bool:
    from infrastructure.db.repositories import OperationalSwitchRepository
    from infrastructure.db.session import session_factory_from_settings

    factory = session_factory_from_settings(settings)
    async with factory() as session:
        return await OperationalSwitchRepository(session).is_on(switch)


async def cmd_daily_paused(settings: Settings, slot_date: str) -> int:
    if not await _db_switch_on(settings, OperationalSwitch.PAUSED):
        print("NG: refusing: DB switch paused is not on", file=sys.stderr)
        return 2
    client = await _client(settings)
    wf_id = f"smoke-workers-daily-{uuid.uuid4().hex[:12]}"
    result = await client.execute_workflow(
        DAILY_EPISODE_WORKFLOW[0],
        DailyEpisodeInput(slot_date=slot_date, topic="smoke-workers (must stay paused)"),
        id=wf_id,
        task_queue=PIPELINE_TASK_QUEUE,
        result_type=DailyEpisodeResult,
        execution_timeout=timedelta(minutes=3),
    )
    print(json.dumps({"workflow_id": wf_id, **result.__dict__}))
    return 0


@workflow.defn(name="SmokeWorkersGateProbe", sandboxed=False)
class GateProbeWorkflow:
    """使い捨て: Activity だけを queue pipeline（compose の pipeline-worker）で実行させる。"""

    @workflow.run
    async def run(self, _: str) -> str:
        opts = {
            "task_queue": PIPELINE_TASK_QUEUE,
            "start_to_close_timeout": timedelta(seconds=30),
            "schedule_to_close_timeout": timedelta(minutes=2),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        }
        paused: CheckPausedResult = await workflow.execute_activity(
            PIPELINE_CHECK_PAUSED,
            CheckPausedRequest(include_uploads=True),
            result_type=CheckPausedResult,
            **opts,  # type: ignore[arg-type]
        )
        gate: UploadGateResult = await workflow.execute_activity(
            PIPELINE_UPLOAD_GATE,
            UploadGateRequest(episode_id=PROBE_EPISODE_ID),
            result_type=UploadGateResult,
            **opts,  # type: ignore[arg-type]
        )
        return json.dumps(
            {
                "check_paused_include_uploads": {"paused": paused.paused, "reason": paused.reason},
                "upload_gate": {"allowed": gate.allowed, "reason": gate.reason},
            }
        )


async def cmd_gate_probe(settings: Settings) -> int:
    client = await _client(settings)
    queue = f"smoke-workers-probe-{uuid.uuid4().hex[:12]}"
    async with Worker(client, task_queue=queue, workflows=[GateProbeWorkflow]):
        result = await client.execute_workflow(
            GateProbeWorkflow.run,
            "probe",
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(minutes=3),
        )
    print(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pollers")
    p.add_argument("--queue", action="append", required=True)
    d = sub.add_parser("daily-paused")
    d.add_argument("--slot-date", default="2000-01-01")
    sub.add_parser("gate-probe")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "pollers":
        return asyncio.run(cmd_pollers(settings, args.queue))
    if args.command == "daily-paused":
        return asyncio.run(cmd_daily_paused(settings, args.slot_date))
    return asyncio.run(cmd_gate_probe(settings))


if __name__ == "__main__":
    raise SystemExit(main())
