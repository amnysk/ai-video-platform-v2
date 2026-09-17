"""upload / render の smoke は、既に同じ queue を poll している Worker と競合しない（ADR-0024）。

host の ``pgrep`` は rootless Docker のコンテナ内プロセスを見られないことがある。
Temporal に poller を問い合わせ（identity を問わない）、居れば拒否する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("script", "queues"),
    [
        ("scripts/smoke-upload.sh", ["upload", "upload-media"]),
        ("scripts/smoke-render.sh", ["render", "render-media"]),
    ],
)
def test_smoke_refuses_when_any_poller_exists(script: str, queues: list[str]) -> None:
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "infrastructure.temporal.poller_check" in text
    assert "--identity-suffix ''" in text
    for queue in queues:
        assert f"--queue {queue}" in text


def test_upload_smoke_does_not_refuse_idle_compose_worker_by_process_name() -> None:
    """host の pgrep は rootless コンテナ内の（poll していない）upload-worker も見えてしまう。

    本物の worker との競合は poller_check が判定する。
    pgrep は host の fake worker の二重起動だけを見る。
    """
    text = (ROOT / "scripts/smoke-upload.sh").read_text(encoding="utf-8")
    assert "workers.upload.run_worker" not in text
    assert "約2分" in text
