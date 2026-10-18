"""Agent Tool の executor の組み立て（ADR-0047 §3）。

``build_tool_executors`` は **有効な（``JPMAS2_ENABLED_TOOLS``）tool の executor だけ**を返す。
executor は外部呼び出しだけを担当する（tool_call_id・保存済み結果の再利用・timeout・記録・ログは
``ToolGateway``）。

各 tool 群（research / analytics）は**自分の ``_*_executors`` 関数の本文だけ**を書き換えて
executor を登録する（関数は互いに離れているので、並行して書いても merge が衝突しない）。
tool の定義（入出力 model・上限）は ``contracts/agent_tool_registry.py`` が唯一の宣言元で、
ここは実装の供給だけを持つ。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agent_tool_registry import TOOL_DEFS
from contracts.agent_tools import ToolId
from domain.agents.tool_gateway import ToolSupport
from domain.agents.tools.ports import ToolExecutor
from infrastructure.agents.tools import analytics, research
from infrastructure.agents.tools.budget import AgentToolCallBudget
from infrastructure.agents.tools.observer import LoggingToolObserver
from infrastructure.config import Settings, enabled_tool_ids
from infrastructure.db.agent_tool_call_repository import AgentToolCallRepository
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from infrastructure.storage.artifact_store import ArtifactStore


@dataclass(frozen=True)
class ToolBuildContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    store: ArtifactStore
    bucket: str
    #: tool 群が必要とする追加の adapter（port 実装）。キーは tool 群が決める
    extras: Mapping[str, Any] = field(default_factory=dict)


#: ``ToolBuildContext.extras`` のキー（起動処理が注入する adapter）
EXTRA_RESEARCH_STARTER = "research_starter"
EXTRA_INSIGHT_PROVIDER = "insight_provider"


def _research_executors(ctx: ToolBuildContext) -> dict[ToolId, ToolExecutor]:
    """search_trends / search_historical_facts / fetch_sources（ADR-0047 §4）。

    起動する starter か channel が無ければ空（gateway が「供給されていない」として拒否する）。
    """
    starter = ctx.extras.get(EXTRA_RESEARCH_STARTER)
    channel_id = ctx.settings.youtube_channel_id
    if starter is None or not channel_id:
        return {}
    gateway = ResearchGateway(
        session_factory=ctx.session_factory,
        store=ctx.store,
        config=GatewayConfig.from_settings(ctx.settings),
    )
    return dict(
        research.build(
            gateway=gateway,
            starter=starter,
            session_factory=ctx.session_factory,
            store=ctx.store,
            channel_id=channel_id,
            provider_mode=ctx.settings.research_provider,
        )
    )


def _analytics_executors(ctx: ToolBuildContext) -> dict[ToolId, ToolExecutor]:
    """get_video_analytics / get_audience_retention / compare_video_performance / ... （§5）。"""
    provider = ctx.extras.get(EXTRA_INSIGHT_PROVIDER)
    if provider is None:
        return {}
    return dict(
        analytics.build(
            provider,
            AgentToolCallBudget(ctx.session_factory),
            daily_cap=ctx.settings.agent_tools_daily_analytics_calls,
        )
    )


def build_tool_executors(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    bucket: str,
    extras: Mapping[str, Any] | None = None,
) -> dict[ToolId, ToolExecutor]:
    """有効な tool の executor。無効・未実装の tool は含めない（gateway が拒否する）。"""
    ctx = ToolBuildContext(settings, session_factory, store, bucket, extras or {})
    enabled = enabled_tool_ids(settings)
    built: dict[ToolId, ToolExecutor] = {}
    for part in (_research_executors, _analytics_executors):
        for tool_id, executor in part(ctx).items():
            if tool_id in built:
                raise ValueError(f"duplicate tool executor: {tool_id.value}")
            built[tool_id] = executor
    return {t: e for t, e in built.items() if t in enabled}


def build_tool_support(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    bucket: str,
    extras: Mapping[str, Any] | None = None,
) -> ToolSupport | None:
    """``JPMAS2_AGENT_TOOLS`` が無効なら ``None``（gateway は従来どおり。何も組み立てない）。"""
    if not settings.jpmas2_agent_tools:
        return None
    return ToolSupport(
        defs=TOOL_DEFS,
        executors=build_tool_executors(
            settings, session_factory=session_factory, store=store, bucket=bucket, extras=extras
        ),
        store=AgentToolCallRepository(session_factory, store),
        observer=LoggingToolObserver(),
    )
