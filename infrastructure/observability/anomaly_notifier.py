"""運用異常の通知口（ADR-0027）。

既定は**ログ（ERROR）だけ**。DB への記録は watchdog が別に行う。将来 Slack / メール等へ繋ぐときは
``AnomalyNotifier`` を実装して差し替える（watchdog は Protocol にだけ依存する）。
通知は「記録の1行につき1回」で、失敗したら次回の検査で再送される。

detail に secret を入れない（INV-20）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from contracts.schedule_guard import AnomalyKind

logger = logging.getLogger("avp.anomaly")


@dataclass(frozen=True, slots=True)
class AnomalyNotice:
    kind: AnomalyKind
    anomaly_date: date
    occurrences: int
    detail: dict[str, Any] = field(default_factory=dict)


class AnomalyNotifier(Protocol):
    async def notify(self, notice: AnomalyNotice) -> None: ...


class LoggingAnomalyNotifier:
    """既定の通知。固定キー ``anomaly=<KIND>`` で grep / ログ監視できる。"""

    async def notify(self, notice: AnomalyNotice) -> None:
        logger.error(
            "OPERATIONAL_ANOMALY anomaly=%s date=%s occurrences=%d detail=%s",
            notice.kind.value,
            notice.anomaly_date.isoformat(),
            notice.occurrences,
            notice.detail,
        )
