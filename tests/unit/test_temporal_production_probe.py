"""本番 Temporal の動的検出（ADR-0079 / INV-121）。読み取り（describe）だけで判定する。"""

from __future__ import annotations

import pytest
from temporalio.service import RPCError, RPCStatusCode

from contracts.pipeline import DAILY_SCHEDULE_ID
from tests.support.temporal import ProductionTemporalError, assert_temporal_is_not_production


class _Handle:
    def __init__(self, outcome: Exception | None, seen: list[str], schedule_id: str) -> None:
        self._outcome, self._seen, self._id = outcome, seen, schedule_id

    async def describe(self):
        self._seen.append(self._id)
        if self._outcome is not None:
            raise self._outcome
        return object()


class _Client:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.described: list[str] = []

    def get_schedule_handle(self, schedule_id: str) -> _Handle:
        return _Handle(self.outcome, self.described, schedule_id)


@pytest.mark.asyncio
async def test_a_server_that_has_the_daily_schedule_is_production() -> None:
    client = _Client()
    with pytest.raises(ProductionTemporalError, match="本番"):
        await assert_temporal_is_not_production("x:7233", client=client)  # type: ignore[arg-type]
    assert client.described == [DAILY_SCHEDULE_ID]  # describe だけ（起動・作成はしない）


@pytest.mark.asyncio
async def test_a_server_without_the_schedule_passes() -> None:
    client = _Client(RPCError("nf", RPCStatusCode.NOT_FOUND, b""))
    await assert_temporal_is_not_production("x:7233", client=client)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        RPCError("denied", RPCStatusCode.PERMISSION_DENIED, b""),
        RPCError("down", RPCStatusCode.UNAVAILABLE, b""),
        RuntimeError("boom"),
    ],
)
async def test_an_undecidable_answer_fails_closed(outcome: Exception) -> None:
    with pytest.raises(ProductionTemporalError):
        await assert_temporal_is_not_production("x:7233", client=_Client(outcome))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_unreachable_server_is_left_to_the_tests_to_skip() -> None:
    await assert_temporal_is_not_production("127.0.0.1:1", timeout=1.0)
