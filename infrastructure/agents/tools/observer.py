"""``ToolObserver`` の構造化ログ実装（ADR-0047 §8、log-contract）。

出すのは ID・状態・時間だけ。**引数・結果の本文・token は渡されもしない**
（``ToolEvent`` が持たない）。
発行の失敗は業務に伝播させない（INV-38）。metrics / traces はこのログの OpenSearch 集計で出す。
"""

from __future__ import annotations

import logging

from contracts.log_contract import EventName
from domain.agents.tools.observer import ToolEvent
from infrastructure.logging.emit import emit, log_guard

logger = logging.getLogger(__name__)


class LoggingToolObserver:
    def completed(self, event: ToolEvent) -> None:
        with log_guard():
            emit(
                logger,
                EventName.AGENT_TOOL_COMPLETED,
                logging.INFO,
                "agent tool %s finished: %s",
                event.tool_id,
                event.status,
                episode_id=event.episode_id or None,
                tool_name=event.tool_id,
                duration_ms=event.duration_ms,
                error_code=event.error_code,
                attributes={
                    k: v
                    for k, v in (
                        ("tool_call_id", event.tool_call_id),
                        ("tool_version", event.tool_version),
                        ("agent_role", event.agent_role),
                        ("status", event.status),
                        ("cost_class", event.cost_class),
                        ("cache_hit", event.cache_hit),
                        ("reason", event.reason),
                    )
                    if v is not None
                },
            )

    def refused(self, event: ToolEvent) -> None:
        with log_guard():
            emit(
                logger,
                EventName.AGENT_TOOL_REFUSED,
                logging.WARNING,
                "agent tool %s refused: %s",
                event.tool_id,
                event.reason,
                episode_id=event.episode_id or None,
                tool_name=event.tool_id,
                error_code=event.reason,
                attributes={
                    k: v
                    for k, v in (
                        ("tool_call_id", event.tool_call_id),
                        ("tool_version", event.tool_version),
                        ("agent_role", event.agent_role),
                        ("status", event.status),
                        ("cost_class", event.cost_class),
                        ("cache_hit", event.cache_hit),
                        ("reason", event.reason),
                    )
                    if v is not None
                },
            )
