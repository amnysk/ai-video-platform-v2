"""tool 呼び出しの観測 port（ADR-0047 §8）。``OperationObserver`` と同形で、既定は何もしない。

OpenTelemetry などへの差し込み口。**引数・結果の本文・token は渡さない**（ID と状態と時間だけ）。
観測の失敗は業務に伝播させない（INV-38。gateway が握る）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ToolEvent:
    tool_id: str
    tool_version: int | None
    agent_role: str
    episode_id: str
    #: 完了したときの ``ToolStatus`` の値。拒否のときは ``None``
    status: str | None = None
    #: 拒否の理由コード（``tool_not_allowed`` 等）。完了のときは ``None``
    reason: str | None = None
    tool_call_id: str | None = None
    duration_ms: int | None = None
    cost_class: str | None = None
    cache_hit: bool | None = None
    error_code: str | None = None


class ToolObserver(Protocol):
    def completed(self, event: ToolEvent) -> None: ...

    def refused(self, event: ToolEvent) -> None: ...


class NoopToolObserver:
    def completed(self, event: ToolEvent) -> None:
        return None

    def refused(self, event: ToolEvent) -> None:
        return None
