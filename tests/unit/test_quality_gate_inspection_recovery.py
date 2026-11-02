"""品質ゲートの検査系の一時障害からの回復（ADR-0087 §8 / INV-156）。

理由は docs/testing/bgm-auto-quality-rationale.md。実の render で作った final_video を使う。
"""

# ruff: noqa: F811 - 別ファイルの fixture（env）を使う
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import exc as sa_exc

from contracts.quality import QualityReason, QualityVerdict
from contracts.quality_records import CheckKey
from domain.quality.inspection import RETRY_POLICY, InspectionFault
from infrastructure.db.models import QualityCheckRecordRow, QualityReportRow
from infrastructure.db.quality_repositories import QualityRecordRepository
from infrastructure.quality.service import ArtifactIntegrityError, QualityGateService
from tests.unit.test_quality_gate_service import (  # noqa: F401 - fixtures
    Env,
    FakeProducer,
    env,
    factory,
)


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FlakyStore:
    """get_json を最初の ``fail_times`` 回だけ失敗させる。他は元のストアに委ねる。"""

    def __init__(self, inner: Any, make_exc: Callable[[], BaseException], fail_times: int) -> None:
        self._inner = inner
        self._make_exc = make_exc
        self.remaining = fail_times
        self.get_calls = 0

    async def get_json(self, key: str) -> Any:
        self.get_calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self._make_exc()
        return await self._inner.get_json(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _service(
    env: Env, store: Any, sleeper: Sleeper, *producers: Any, now: Callable[[], float] = lambda: 0.0
) -> QualityGateService:
    return QualityGateService(
        session_factory=env.factory,
        store=store,
        producers=producers,
        destination_id="UC" + "a" * 22,
        clock=env.clock,
        sleep=sleeper,
        monotonic=now,
    )


async def _run(service: QualityGateService, env: Env):
    return await service.evaluate(env.episode_id, manual=False)


async def test_a_transient_store_fault_is_retried_and_the_episode_recovers(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    store = FlakyStore(env.store, lambda: ConnectionResetError("minio reset"), fail_times=2)
    sleeper = Sleeper()
    outcome = await _run(_service(env, store, sleeper), env)
    assert outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.PASS
    assert outcome.recovered_faults == 2
    assert sleeper.calls == [1.0, 2.0]  # 指数バックオフ


async def test_a_persistent_transient_fault_is_review_with_inspection_unavailable(
    env: Env,
) -> None:
    await env.declare(bgm=False, map_=False)
    store = FlakyStore(env.store, lambda: TimeoutError("slow"), fail_times=10**6)
    sleeper = Sleeper()
    outcome = await _run(_service(env, store, sleeper), env)
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.REVIEW  # PASS に倒れない・BLOCK でもない
    assert outcome.assessment.decision.reason_code is QualityReason.INSPECTION_UNAVAILABLE
    assert "technical_qa" in outcome.unavailable
    # 1 つの producer は 3 回までしか試さない（3 回 + 内蔵の 2 producer が各 3 回）
    assert len(sleeper.calls) <= 2 * 2 + 2  # 待ちは producer ごとに 2 回以内
    assert sum(sleeper.calls) <= 2 * (1.0 + 2.0)
    # 報告にも区別が残る
    (row,) = await env.rows(QualityReportRow)
    reasons = {c["reason_code"] for c in row.report_json["checks"]}
    assert QualityReason.INSPECTION_UNAVAILABLE.value in reasons
    assert QualityReason.REQUIRED_CHECK_MISSING.value not in reasons
    assert row.verdict == "review"
    assert await env.rows(QualityCheckRecordRow) == []  # 偽の記録は書かない


async def test_a_permanent_defect_is_not_retried_and_stays_a_quality_review(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    store = FlakyStore(env.store, lambda: ArtifactIntegrityError("hash mismatch"), fail_times=10**6)
    sleeper = Sleeper()
    outcome = await _run(_service(env, store, sleeper), env)
    assert sleeper.calls == []
    assert outcome.recovered_faults == 0
    assert outcome.unavailable == ()
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_NEEDS_REVIEW
    (row,) = await env.rows(QualityReportRow)
    reasons = {c["reason_code"] for c in row.report_json["checks"]}
    assert QualityReason.REQUIRED_CHECK_MISSING.value in reasons
    assert QualityReason.INSPECTION_UNAVAILABLE.value not in reasons


async def test_the_total_budget_stops_retrying_before_thirty_seconds(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    store = FlakyStore(env.store, lambda: OSError(5, "io"), fail_times=10**6)
    sleeper = Sleeper()
    ticks = iter(range(0, 10_000, 31))  # 呼ぶたびに 31 秒進む時計
    outcome = await _run(_service(env, store, sleeper, now=lambda: float(next(ticks))), env)
    assert sleeper.calls == []  # 最初の待ちで既に予算 30 秒を超えるので待たずに諦める
    assert outcome.assessment.decision.reason_code is QualityReason.INSPECTION_UNAVAILABLE


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: ConnectionResetError(),
        lambda: TimeoutError(),
        lambda: OSError(5, "io"),
        lambda: InspectionFault("x"),
        lambda: sa_exc.OperationalError("s", {}, Exception("down")),
        lambda: ArtifactIntegrityError("x"),
        lambda: ValueError("x"),
        lambda: RuntimeError("x"),
    ],
)
@pytest.mark.parametrize("fail_times", [1, 2, 3, 10**6])
async def test_no_failure_pattern_ever_yields_an_allowance_unless_recovered_cleanly(
    env: Env, make_exc: Callable[[], BaseException], fail_times: int
) -> None:
    await env.declare(bgm=False, map_=False)
    store = FlakyStore(env.store, make_exc, fail_times=fail_times)
    outcome = await _run(_service(env, store, Sleeper()), env)
    if outcome.assessment.decision.allowed:
        # 許可は、再試行で全ての必須検査が実際に記録できたときだけ
        assert outcome.assessment.verdict in (QualityVerdict.PASS, QualityVerdict.WARN)
        assert outcome.unavailable == ()
        assert {r.check_key for r in await env.rows(QualityCheckRecordRow)} == {
            "technical_qa",
            "description_lint",
        }
    else:
        assert outcome.assessment.decision.reason_code in (
            QualityReason.INSPECTION_UNAVAILABLE,
            QualityReason.REPORT_NEEDS_REVIEW,
            QualityReason.REPORT_EVALUATION_ERROR,
        )


async def test_a_genuine_block_is_not_hidden_behind_inspection_unavailable(env: Env) -> None:
    """片方が検査系の故障でも、もう片方が本物の BLOCK なら BLOCK の理由が残る。"""
    await env.declare(bgm=True, map_=False)
    bad = FakeProducer(CheckKey.BGM, QualityVerdict.BLOCK)
    store = FlakyStore(env.store, lambda: TimeoutError(), fail_times=10**6)
    outcome = await _run(_service(env, store, Sleeper(), bad), env)
    # store が落ちているので技術 QA は unavailable。BGM の producer は BLOCK を記録する
    assert outcome.assessment.verdict is QualityVerdict.BLOCK
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_BLOCKED


async def test_a_transient_db_fault_while_saving_the_report_is_retried(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.declare(bgm=False, map_=False)
    original = QualityRecordRepository.save_report
    state = {"n": 0}

    async def flaky(self, report):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] <= 1:
            raise sa_exc.OperationalError("s", {}, Exception("conn lost"))
        return await original(self, report)

    monkeypatch.setattr(QualityRecordRepository, "save_report", flaky)
    outcome = await _run(_service(env, env.store, Sleeper()), env)
    assert outcome.assessment.decision.allowed
    assert outcome.recovered_faults == 1
    assert len(await env.rows(QualityReportRow)) == 1  # 二重に保存しない


async def test_a_persistent_db_fault_while_saving_is_inspection_unavailable_not_allowed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.declare(bgm=False, map_=False)

    async def down(self, report):  # type: ignore[no-untyped-def]
        raise sa_exc.OperationalError("s", {}, Exception("db down"))

    monkeypatch.setattr(QualityRecordRepository, "save_report", down)
    outcome = await _run(_service(env, env.store, Sleeper()), env)
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.decision.reason_code is QualityReason.INSPECTION_UNAVAILABLE
    assert outcome.report_id is None


async def test_a_permanent_db_error_while_saving_keeps_the_old_reason(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.declare(bgm=False, map_=False)

    async def bad(self, report):  # type: ignore[no-untyped-def]
        raise sa_exc.IntegrityError("s", {}, Exception("dup"))

    monkeypatch.setattr(QualityRecordRepository, "save_report", bad)
    outcome = await _run(_service(env, env.store, Sleeper()), env)
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_PRODUCTION_FAILED


async def test_a_transient_failure_reading_records_is_unavailable_not_evaluation_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(self, subject):  # type: ignore[no-untyped-def]
        raise sa_exc.OperationalError("s", {}, Exception("db down"))

    monkeypatch.setattr(QualityRecordRepository, "list_check_sets", down)
    sleeper = Sleeper()
    outcome = await _run(_service(env, env.store, sleeper), env)
    assert outcome.assessment.decision.reason_code is QualityReason.INSPECTION_UNAVAILABLE
    assert not outcome.assessment.decision.allowed
    assert sleeper.calls == [1.0, 2.0]


def test_the_default_policy_is_the_adr_policy() -> None:
    assert (RETRY_POLICY.attempts, RETRY_POLICY.max_total_seconds) == (3, 30.0)
