"""Activity が出す業務イベント（upload・render 検証・日次枠・research・運用異常）。

理由は docs/testing/logging-rationale.md。どれも既存の制御・文言は変えず、照合用の事実を足す。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from contracts.research import ResearchWorkflowOutput
from contracts.schedule_guard import AnomalyKind
from infrastructure.observability.anomaly_notifier import AnomalyNotice, LoggingAnomalyNotifier
from tests.support.fake_youtube import FAKE_SESSION_PREFIX
from tests.support.log_capture import capture_json
from tests.unit.test_upload_activities import (  # noqa: F401 — fixture を使う
    Harness,
    _admitted,
    factory,
    h,
)
from workers.render.activities import _log_validation
from workers.research.activities import _finished


async def test_upload_started_succeeded_then_reused_existing(h: Harness) -> None:  # noqa: F811
    acts = await _admitted(h)
    # 本番の既定レベル（INFO）で見る。DEBUG の SQL ログ（aiosqlite）は setup が WARNING に固定する
    with capture_json(logging.INFO) as logs:
        first = await h.upload(acts)
    names = logs.names()
    assert names.index("upload.started") < names.index("upload.succeeded")
    [ok] = logs.events("upload.succeeded")
    assert ok["episode_id"] == h.episode_id
    assert ok["attributes"]["video_id"] == first.video_id
    assert ok["stage"] == "upload"
    # session URI（capability）は出さない（INV-20）
    assert FAKE_SESSION_PREFIX not in logs.stream.getvalue()

    with capture_json() as logs:
        again = await h.upload(h.activities())
    assert again.skipped
    [reused] = logs.events("upload.reused_existing")
    assert reused["attributes"]["video_id"] == first.video_id
    assert reused["outcome"] == "reused"


@dataclass
class _Check:
    check: str
    passed: bool
    detail: str


def test_render_validation_reports_failed_checks() -> None:
    with capture_json() as logs:
        _log_validation(
            "ep", [_Check("duration", True, ""), _Check("audio", False, "overlap")], "x"
        )
        _log_validation("ep", [_Check("duration", True, "")], "final")
    [failed] = logs.events("render.validation.failed")
    assert failed["attributes"]["failed_checks"] == [{"check": "audio", "detail": "overlap"}]
    assert failed["stage"] == "render" and failed["level"] == "WARNING"
    assert logs.events("render.validation.passed")[0]["attributes"]["phase"] == "final"


def test_research_finished_carries_the_research_request_id() -> None:
    with capture_json() as logs:
        _finished(ResearchWorkflowOutput(request_id="rr-1", status="partial", searches=3))
    [done] = logs.events("research.request.finished")
    assert done["research_request_id"] == "rr-1"
    assert "request_id" not in done  # API の request_id と混ぜない
    assert done["outcome"] == "failed" and done["attributes"]["status"] == "partial"


async def test_anomaly_keeps_the_grep_key_and_adds_the_event() -> None:
    notice = AnomalyNotice(
        kind=next(iter(AnomalyKind)), anomaly_date=date(2026, 9, 30), occurrences=2
    )
    with capture_json() as logs:
        await LoggingAnomalyNotifier().notify(notice)
    [event] = logs.events("anomaly.recorded")
    assert event["message"].startswith("OPERATIONAL_ANOMALY anomaly=")
    assert event["level"] == "ERROR"
