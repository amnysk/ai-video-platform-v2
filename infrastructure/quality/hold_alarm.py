"""gate の判定の保留率の警報を ``operational_anomalies`` に記録する（ADR-0087 §11）。

自動経路の判定（``upload_gate``）の直後に呼ぶ。直近の報告（``quality_reports``）から判定を復元し、
``recent_hold_alarm`` が警報なら ``QUALITY_GATE_HOLD_RATE_HIGH`` を日ごとに 1 行（同日は件数だけ
増やす）記録する。警報が止んだら今日の行を解決する。

**gate を失敗させない**: どんな例外も飲み込んでログに残すだけ。判定・Episode の状態には触れない。
履歴は保存できた報告だけ（報告を保存できなかった判定は数えない。DB が落ちているときは警報も
書けないため、その場合は ``quality.decision`` の構造化ログが唯一の記録）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.quality import QualityVerdict
from contracts.schedule_guard import AnomalyKind
from domain.quality.hold_rate import (
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
    GateDecisionSample,
    HoldAlarm,
    recent_hold_alarm,
    sample_from_report,
)
from infrastructure.db.models import QualityReportRow
from infrastructure.db.repositories import OperationalAnomalyRepository

logger = logging.getLogger(__name__)


async def load_recent_samples(
    session: AsyncSession, *, window: int = DEFAULT_WINDOW
) -> list[GateDecisionSample]:
    """直近 ``window`` 件の判定（古い順）。"""
    rows = (
        await session.execute(
            select(QualityReportRow.verdict, QualityReportRow.report_json)
            .order_by(QualityReportRow.generated_at.desc(), QualityReportRow.created_at.desc())
            .limit(window)
        )
    ).all()
    samples: list[GateDecisionSample] = []
    for verdict, report_json in reversed(rows):
        codes = [str(c.get("reason_code", "")) for c in (report_json or {}).get("checks", []) if c]
        samples.append(sample_from_report(QualityVerdict(verdict), codes))
    return samples


async def record_hold_rate_alarm(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    window: int = DEFAULT_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
) -> HoldAlarm | None:
    """警報があれば記録して返す。例外は投げない。"""
    try:
        today = now.astimezone(UTC).date()
        async with session_factory() as session:
            samples = await load_recent_samples(session, window=window)
            alarm = recent_hold_alarm(samples, window=window, threshold=threshold)
            anomalies = OperationalAnomalyRepository(session)
            if alarm is None:
                await anomalies.resolve(AnomalyKind.QUALITY_GATE_HOLD_RATE_HIGH, today, now=now)
            else:
                await anomalies.record(
                    AnomalyKind.QUALITY_GATE_HOLD_RATE_HIGH, today, alarm.to_detail(), now=now
                )
                logger.error(
                    "anomaly=%s date=%s holds=%d/%d quality=%d inspection=%d",
                    AnomalyKind.QUALITY_GATE_HOLD_RATE_HIGH.value,
                    today.isoformat(),
                    alarm.holds,
                    alarm.window,
                    alarm.quality_holds,
                    alarm.inspection_holds,
                )
            await session.commit()
            return alarm
    except Exception:  # noqa: BLE001 - 警報の失敗で gate を止めない
        logger.warning("quality hold-rate alarm could not be recorded", exc_info=True)
        return None
