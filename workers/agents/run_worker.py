"""Agent worker のエントリポイント（ADR-0043 §6）。

queue ``agents`` に Activity ``run_agent_stage`` を登録する（workflow は持たない）。
``JPMAS2_AGENTIC_WORKFLOWS`` が true のときだけ起動する（INV-55）。無効なら理由を出して終了する。
compose では ``agentic`` profile だけ（``core`` では起動しない）。

LLM は ``JPMAS2_AGENT_LLM``: ``codex``（既定。``CodexCliStoryGenerator`` を注入）か、
隔離試験スタック専用の決定的な ``fake``（外部へ何も出ない）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.agentic_pipeline import AGENTS_TASK_QUEUE
from contracts.agents import AgentSpec, ModelPolicy
from domain.agents.ports import AgentLlm
from infrastructure.agents.codex_llm import CodexAgentLlm
from infrastructure.agents.fake_llm import FakeAgentLlm
from infrastructure.agents.tools import build_tool_support
from infrastructure.agents.tools.extras import build_tool_extras
from infrastructure.config import Settings, enabled_agent_roles
from infrastructure.db.session import session_factory_from_settings
from infrastructure.intervention.budget import budget_policy_from_settings
from infrastructure.intervention.internal_port import SessionCommandPort
from infrastructure.logging.temporal import worker_interceptors
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.process import SubprocessRunner
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.agents.activities import AgentActivities
from workers.agents.starter import TemporalCommandStarter

logger = logging.getLogger(__name__)

DISABLED_MESSAGE = (
    "JPMAS2_AGENTIC_WORKFLOWS is not true: the agent worker registers nothing and exits "
    "(agentic features are off by default / INV-55)"
)


def build_worker(client: Client, activities: AgentActivities) -> Worker:
    return Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=AGENTS_TASK_QUEUE,
        activities=activities.activities(),
    )


def llm_factory_for(settings: Settings) -> Callable[[str, AgentSpec, ModelPolicy], AgentLlm]:
    if settings.jpmas2_agent_llm == "fake":
        logger.warning("JPMAS2_AGENT_LLM=fake: agents use a deterministic offline LLM")

        def fake(_episode_id: str, _spec: AgentSpec, _policy: ModelPolicy) -> AgentLlm:
            return FakeAgentLlm(_no_canned_response)

        return fake

    binary = resolve_codex_binary(settings.codex_binary)
    workspace = Path(settings.codex_workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    generator = CodexCliStoryGenerator(
        binary=binary,
        model=settings.codex_model,
        runner=SubprocessRunner(),
        workspace=workspace,
    )

    def codex(episode_id: str, spec: AgentSpec, _policy: ModelPolicy) -> AgentLlm:
        return CodexAgentLlm(
            generator=generator,
            episode_id=episode_id,
            timeout_seconds=min(spec.timeout_seconds, settings.codex_timeout_seconds),
        )

    return codex


def _no_canned_response(prompt: str, schema: dict[str, object]) -> dict[str, object]:
    """``fake`` の既定: 台本の無い応答は返さない（黙って空の成果物を作らない）。

    隔離 E2E は ``llm_factory`` を差し替えて canned 応答を持たせる。
    """
    raise LookupError("the fake agent LLM has no canned response configured")


async def main() -> None:
    settings = Settings()
    if not settings.jpmas2_agentic_workflows:
        sys.exit(DISABLED_MESSAGE)
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    session_factory = session_factory_from_settings(settings)
    budget_policy = budget_policy_from_settings(settings)
    http_client = httpx.AsyncClient(timeout=30.0)
    if budget_policy.usd_jpy_rate is None:
        logger.warning(
            "BUDGET_USD_JPY_RATE is not set: agent stages are blocked as budget_unverifiable "
            "until it is configured"
        )
    activities = AgentActivities(
        session_factory=session_factory,
        store=store,
        bucket=settings.minio_bucket,
        agentic_enabled=settings.jpmas2_agentic_workflows,
        enabled_roles=enabled_agent_roles(settings),
        budget_policy=budget_policy,
        llm_factory=llm_factory_for(settings),
        command_port=SessionCommandPort(
            session_factory, TemporalCommandStarter(client, session_factory)
        ),
        # JPMAS2_AGENT_TOOLS が無効なら None（gateway は従来どおり。ADR-0047）
        tool_support=build_tool_support(
            settings,
            session_factory=session_factory,
            store=store,
            bucket=settings.minio_bucket,
            extras=(
                build_tool_extras(settings, temporal_client=client, http_client=http_client)
                if settings.jpmas2_agent_tools
                else None
            ),
        ),
    )
    worker = build_worker(client, activities)
    logger.info(
        "agent worker listening on %s (llm=%s, enabled roles=%s)",
        AGENTS_TASK_QUEUE,
        settings.jpmas2_agent_llm,
        sorted(r.value for r in enabled_agent_roles(settings)),
    )
    async with worker:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
