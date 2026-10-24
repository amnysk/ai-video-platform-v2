"""日次の入口ゲートと保留の語彙（ADR-0058）。単一宣言元（AGENTS.md §8）。

``contracts/provider_health.py``（ADR-0057: breaker・保留の語彙）を使う側の契約。日次
workflow・pipeline workflow・watchdog が共有するのはこの名前と型だけ（INV-3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

#: ``DailyEpisodeWorkflow`` に入口ゲートと Codex 事前診断を足した patch id（旧履歴は旧経路）
DAILY_PREFLIGHT_PATCH_ID = "daily-preflight-0058"

PIPELINE_DAILY_GATE = "pipeline_daily_gate"
PIPELINE_PRODUCTION_GATE = "pipeline_production_gate"

#: 保留がこの日数を超えたら watchdog が既存の ``EPISODE_STAGE_STALLED`` で通報する
#: （新しい AnomalyKind を足さない: kind の CHECK を広げない / ADR-0058）
DEFAULT_HOLD_ESCALATION_DAYS = 3


class DailyRunMode(StrEnum):
    """入口ゲートが決める、その日の実行の*モード*（Schedule が発火するかどうかではない）。"""

    NORMAL = "normal"
    #: breaker が closed でない。企画・台本・絵コンテは進め、production の前で保留する
    PAID_HELD = "paid_held"
    #: 保留が上限に達している。新しい Episode を作らない（Planner も Codex も呼ばない）
    SKIP_BACKLOG = "skip_backlog"


@dataclass
class DailyGateRequest:
    slot_date: str


@dataclass
class DailyGateResult:
    mode: str
    #: 読んだ breaker の状態（アカウントごとの最も厳しいもの）。結線前は "closed"
    breaker_state: str = "closed"
    open_hold_count: int = 0
    #: 未照合の予約数（情報。新しい Episode は止めない。再開は日次がしない）
    unreconciled_reservations: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class ProductionGateRequest:
    episode_id: str


@dataclass
class ProductionGateResult:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OpenHold:
    """未解放の保留 1 件（Episode 単位に畳んだもの）。watchdog が分類に使う。"""

    episode_id: str
    held_since: datetime
    #: ``HoldReason`` の値
    reason: str
    account_key: str
    resumable_stage: str


class OpenHoldReader(Protocol):
    """未解放の保留を返す port。実装は保留テーブルの未解放行を読むだけ（ADR-0057 側）。

    ``ProviderHealthReader.open_hold_count`` は件数しか返さず、どの Episode が保留かを知る手段が
    無いので、watchdog 用に最小の port を足している。
    """

    async def list_open_holds(self) -> list[OpenHold]: ...
