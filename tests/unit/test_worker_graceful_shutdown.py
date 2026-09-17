"""render / upload の Worker は停止時に実行中の Activity の完了を待つ（ADR-0024）。

``graceful_shutdown_timeout`` が 0（SDK 既定）だと ``docker compose stop`` で即 cancel され、
compose の ``stop_grace_period`` が意味を持たない。猶予は stop_grace_period（120s）より短くする。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from infrastructure.config import Settings

GRACE = timedelta(seconds=100)


class _Recorder:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def __call__(self, client, **kwargs):
        self.created.append(kwargs)
        return object()


def test_render_workers_wait_for_in_flight_activities(monkeypatch: pytest.MonkeyPatch) -> None:
    import workers.render.run_worker as rw
    from workers.render.activities import RenderActivities

    recorder = _Recorder()
    monkeypatch.setattr(rw, "Worker", recorder)
    rw.build_workers(object(), Settings(), RenderActivities.__new__(RenderActivities))  # type: ignore[arg-type]
    assert [kw.get("graceful_shutdown_timeout") for kw in recorder.created] == [GRACE, GRACE]


def test_upload_workers_wait_for_in_flight_activities(monkeypatch: pytest.MonkeyPatch) -> None:
    import workers.upload.run_worker as uw
    from workers.upload.activities import UploadActivities

    recorder = _Recorder()
    monkeypatch.setattr(uw, "Worker", recorder)
    uw.build_workers(object(), UploadActivities.__new__(UploadActivities))  # type: ignore[arg-type]
    assert [kw.get("graceful_shutdown_timeout") for kw in recorder.created] == [GRACE, GRACE]
