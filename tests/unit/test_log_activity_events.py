"""Activity が出す業務イベント（upload・render 検証・日次枠・research・運用異常）。

理由は docs/testing/logging-rationale.md。どれも既存の制御・文言は変えず、照合用の事実を足す。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from contracts.research import ResearchWorkflowOutput
from contracts.schedule_guard import AnomalyKind
from contracts.states import ReservationStatus
from domain.errors import InvalidTransitionError
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
from workers.upload import activities as upload_activities


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


async def test_an_attempt_that_finds_the_result_already_recorded_logs_reuse(
    h: Harness,  # noqa: F811
) -> None:
    """並行2試行の一方が予約を取った**後**に、他方が投稿して spent を記録した（順序を固定）。

    動画は1本。``upload.succeeded`` は送った試行の1件だけで、後から台帳の spent を見た試行は
    ``upload.reused_existing``（``found_at=record``。YouTube へ送っていない）、``reservation.spent``
    も1件（no-op の再記録では出さない）。レビュー I-20（担当C V-3）・I-21（並行のまま走らせると
    負けた側が job の遷移で止まる回があり、この経路に届くかがタイミング次第だった）。
    """
    await _admitted(h)
    winner, loser = h.activities(), h.activities()
    reserve = loser._reserve

    async def reserve_then_let_the_winner_finish(*args: Any, **kwargs: Any) -> Any:
        reservation = await reserve(*args, **kwargs)
        assert reservation.status is ReservationStatus.RESERVED
        await h.upload(winner, "run-1")  # 負けた側が予約を読んだ後に、勝った側が投稿し spent を記録
        return reservation

    loser._reserve = reserve_then_let_the_winner_finish  # type: ignore[method-assign]
    with capture_json(logging.INFO) as logs:
        result = await h.upload(loser, "run-1")
    assert h.fake.videos_created == 1
    (reservation,) = await h.reservations()
    assert reservation.status is ReservationStatus.SPENT
    succeeded = logs.events("upload.succeeded")
    reused = logs.events("upload.reused_existing")
    assert len(succeeded) == 1, [e["attributes"] for e in succeeded]
    assert len(reused) == 1
    assert reused[0]["attributes"]["found_at"] == "record"
    assert succeeded[0]["attributes"]["video_id"] == result.video_id
    assert reused[0]["attributes"]["video_id"] == result.video_id
    assert len(logs.events("reservation.spent")) == 1


async def test_an_attempt_rejected_by_the_job_transition_logs_no_upload_outcome(
    h: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """並行2試行の一方が job を ``queued`` と読んだ直後に、他方が job を ``running`` にした
    （インターリーブを固定）。読んだ側は job の遷移規則（running + started）で拒否され、投稿に
    入らない（業務の規則のまま）。その試行は upload の結果を何も出さない: ``upload.succeeded`` は
    投稿した試行の1件、``upload.reused_existing`` 0、``reservation.spent`` 1（レビュー I-21。D が
    30回に1回見た経路）。
    """
    await _admitted(h)
    winner, loser = h.activities(), h.activities()
    entered, winner_started = asyncio.Event(), asyncio.Event()
    armed = {"first_read": True}
    original_get = upload_activities.JobRepository.get

    async def get_then_wait(self: Any, job_id: Any) -> Any:
        job = await original_get(self, job_id)
        if armed["first_read"]:  # 負けた側の最初の読み取り（queued）だけ止める
            armed["first_read"] = False
            entered.set()
            await winner_started.wait()
        return job

    monkeypatch.setattr(upload_activities.JobRepository, "get", get_then_wait)
    upload = winner._upload

    async def mark_started(*args: Any, **kwargs: Any) -> Any:
        winner_started.set()  # 勝った側の job.start は commit 済み
        return await upload(*args, **kwargs)

    winner._upload = mark_started  # type: ignore[method-assign]
    with capture_json(logging.INFO) as logs:
        losing = asyncio.create_task(h.upload(loser, "run-1"))
        await entered.wait()
        won = await h.upload(winner, "run-1")
        with pytest.raises(InvalidTransitionError, match="running"):
            await losing
    assert h.fake.videos_created == 1
    (reservation,) = await h.reservations()
    assert reservation.status is ReservationStatus.SPENT
    succeeded = logs.events("upload.succeeded")
    assert len(succeeded) == 1, [e["attributes"] for e in succeeded]
    assert succeeded[0]["attributes"]["video_id"] == won.video_id
    assert logs.events("upload.reused_existing") == []
    assert len(logs.events("reservation.spent")) == 1
