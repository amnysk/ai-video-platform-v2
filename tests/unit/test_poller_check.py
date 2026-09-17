"""infrastructure.temporal.poller_check の単体検査。

実装者向け: 設計書の API に加え、以下の注入点を持つこと。
- ``queues_missing_pollers(pollers_by_queue, *, suffix, now, max_age) -> list[str]``
    * last_access が None の poller は「生存を確認できない」ので missing 扱い。
    * max_age は ``datetime.timedelta``。``now - last_access > max_age`` なら stale。
    * 戻り値は入力 dict の順序で、missing の queue のみ。
- ``async def check(queues, *, describe, suffix, now, max_age) -> list[str]``
    * ``describe`` は ``async (queue: str) -> list[tuple[str, datetime | None]]``
      （workflow/activity 両方の poller を合算したもの）。
- ``def main(argv: list[str] | None = None, *, describe_factory=...) -> int``
  （同期。内部で asyncio.run）
    * ``describe_factory(address: str, namespace: str)`` は上記 describe を返す（同期呼び出し）。
      address/namespace は Settings 由来。
    * ``--queue`` 複数, ``--identity-suffix``（既定 "@"+hostname）,
      ``--max-age-seconds``（既定 120）。
    * 0: 全 queue OK / 1: missing あり（missing の queue 名だけを stdout に出す）/
      2: describe（または factory）が例外（接続不可）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from infrastructure.temporal.poller_check import check, main, queues_missing_pollers

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
AGE = timedelta(seconds=120)
ME = "@render-worker-host"


def _missing(pollers):
    return queues_missing_pollers(pollers, suffix=ME, now=NOW, max_age=AGE)


def test_ok_when_own_identity_fresh() -> None:
    assert _missing({"render": [(f"123{ME}", NOW - timedelta(seconds=5))]}) == []


def test_missing_when_only_other_hosts() -> None:
    assert _missing({"render": [("123@other-host", NOW)]}) == ["render"]


def test_stale_last_access_is_missing() -> None:
    assert _missing({"render": [(f"1{ME}", NOW - timedelta(seconds=121))]}) == ["render"]


def test_none_last_access_is_missing() -> None:
    assert _missing({"render": [(f"1{ME}", None)]}) == ["render"]


def test_multiple_queues_report_only_missing_in_input_order() -> None:
    pollers = {
        "upload-media": [],
        "render": [(f"1{ME}", NOW)],
        "upload": [("1@elsewhere", NOW)],
    }
    assert _missing(pollers) == ["upload-media", "upload"]


async def test_check_uses_describe() -> None:
    seen: list[str] = []

    async def describe(queue: str):
        seen.append(queue)
        return [(f"1{ME}", NOW)] if queue == "render" else []

    result = await check(
        ["render", "render-media"], describe=describe, suffix=ME, now=NOW, max_age=AGE
    )
    assert result == ["render-media"]
    assert seen == ["render", "render-media"]


def _factory(pollers_by_queue=None, exc: BaseException | None = None):
    def factory(address: str, namespace: str):
        async def describe(queue: str):
            if exc is not None:
                raise exc
            return (pollers_by_queue or {}).get(queue, [])

        return describe

    return factory


def test_main_exit_0_when_all_ok() -> None:
    fresh = [(f"9{ME}", datetime.now(UTC))]
    code = main(
        ["--queue", "render", "--queue", "render-media", "--identity-suffix", ME],
        describe_factory=_factory({"render": fresh, "render-media": fresh}),
    )
    assert code == 0


def test_main_exit_1_prints_missing(capsys: pytest.CaptureFixture[str]) -> None:
    fresh = [(f"9{ME}", datetime.now(UTC))]
    code = main(
        ["--queue", "render", "--queue", "render-media", "--identity-suffix", ME],
        describe_factory=_factory({"render": fresh}),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "render-media" in out
    assert out.split() == ["render-media"]


def test_main_exit_2_when_temporal_unreachable() -> None:
    code = main(
        ["--queue", "render", "--identity-suffix", ME],
        describe_factory=_factory(exc=ConnectionError("unreachable")),
    )
    assert code == 2
