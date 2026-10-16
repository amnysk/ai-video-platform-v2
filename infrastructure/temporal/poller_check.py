"""Worker コンテナの healthcheck: 自ホストの poller が各 task queue に居るかを確かめる。

``python -m infrastructure.temporal.poller_check --queue render --queue render-media``
終了コード 0: 全 queue OK / 1: poller 不在の queue あり（queue 名のみ出力）/
2: Temporal に届かない。
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

Poller = tuple[str, datetime | None]
Describe = Callable[[str], Awaitable[Sequence[Poller]]]
DescribeFactory = Callable[[str, str], Describe]

CONNECT_TIMEOUT_SECONDS = 10.0


def queues_missing_pollers(
    pollers_by_queue: Mapping[str, Sequence[Poller]],
    *,
    suffix: str,
    now: datetime,
    max_age: timedelta,
) -> list[str]:
    missing: list[str] = []
    for queue, pollers in pollers_by_queue.items():
        alive = any(
            identity.endswith(suffix) and last is not None and now - last <= max_age
            for identity, last in pollers
        )
        if not alive:
            missing.append(queue)
    return missing


async def check(
    queues: Sequence[str],
    *,
    describe: Describe,
    suffix: str,
    now: datetime,
    max_age: timedelta,
) -> list[str]:
    pollers = {queue: await describe(queue) for queue in queues}
    return queues_missing_pollers(pollers, suffix=suffix, now=now, max_age=max_age)


def temporal_describe_factory(address: str, namespace: str) -> Describe:
    """実 Temporal の DescribeTaskQueue（workflow/activity 両型の poller を合算）。"""
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
    from temporalio.client import Client

    client: Client | None = None

    async def describe(queue: str) -> list[Poller]:
        nonlocal client
        if client is None:
            client = await asyncio.wait_for(
                Client.connect(address, namespace=namespace), timeout=CONNECT_TIMEOUT_SECONDS
            )
        result: list[Poller] = []
        for queue_type in (
            TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ):
            resp = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=namespace,
                    task_queue=TaskQueue(name=queue),
                    task_queue_type=queue_type,
                ),
                timeout=timedelta(seconds=CONNECT_TIMEOUT_SECONDS),
            )
            for poller in resp.pollers:
                last = (
                    poller.last_access_time.ToDatetime(tzinfo=UTC)
                    if poller.HasField("last_access_time")
                    else None
                )
                result.append((poller.identity, last))
        return result

    return describe


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", action="append", required=True)
    parser.add_argument("--identity-suffix", default="@" + socket.gethostname())
    parser.add_argument("--max-age-seconds", type=float, default=120.0)
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None, *, describe_factory: DescribeFactory = temporal_describe_factory
) -> int:
    args = _parse(argv)
    try:
        from infrastructure.config import Settings

        settings = Settings()
        describe = describe_factory(settings.temporal_address, settings.temporal_namespace)
        missing = asyncio.run(
            check(
                args.queue,
                describe=describe,
                suffix=args.identity_suffix,
                now=datetime.now(UTC),
                max_age=timedelta(seconds=args.max_age_seconds),
            )
        )
    except Exception as exc:
        print(f"temporal unreachable: {type(exc).__name__}", file=sys.stderr)
        return 2
    if missing:
        print("\n".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
