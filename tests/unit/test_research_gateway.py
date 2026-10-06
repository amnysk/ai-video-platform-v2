"""Research Gateway（ADR-0037 §5 / §6 / §8）。

冪等な受け付け、鮮度キャッシュ（再利用前に本体を検証）、予算の fail-closed、blocked の再開。
SQLite とインメモリの ArtifactStore で走る。

理由は docs/testing/research-execution-rationale.md。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from contracts.research import (
    ResearchArtifactRef,
    ResearchArtifactType,
    ResearchCoverage,
    ResearchResult,
    ResearchStatus,
    parse_research_spec,
    parse_research_submit,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.research.errors import ResearchIdempotencyConflictError
from domain.research.keys import research_artifact_object_key
from infrastructure.db.models import Base, ResearchCallRow, ResearchRequestRow
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from tests.support.research import evidence_payload, trend_payload

FAKE = GatewayConfig(provider_mode="fake", provider_is_real=False, provider_configured=True)
NONE = GatewayConfig(provider_mode="none", provider_is_real=False, provider_configured=False)
#: 実 Provider の代役（registry は実 Provider を組まないので、設定だけで「実物」を表す）
REAL = GatewayConfig(provider_mode="real-stub", provider_is_real=True, provider_configured=True)


def _gateway(session_factory, store, config: GatewayConfig = FAKE, now: datetime | None = None):
    """時計は既定で壁時計（``finished_at`` はリポジトリが壁時計で書くので、窓も同じ時計で測る）。"""
    return ResearchGateway(
        session_factory=session_factory,
        store=store,
        config=config,
        clock=(lambda: now) if now is not None else (lambda: datetime.now(UTC)),
    )


def _submit(key: str, payload: dict | None = None):
    return parse_research_submit({**(payload or evidence_payload()), "idempotency_key": key})


async def _count(session_factory, row: type[Base] = ResearchRequestRow) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(row)) or 0)


async def _complete(
    session_factory,
    store,
    request_id: str,
    artifact_type: ResearchArtifactType = ResearchArtifactType.RESEARCH_EVIDENCE,
) -> str:
    """依頼を ``completed`` にする（成果物を書いて記録してから状態を進める）。object key を返す。"""
    payload = {"request_id": request_id, "finding": "the bridge opened in 1883"}
    sha = sha256_hex(canonical_json_bytes(payload))
    key = research_artifact_object_key(request_id, artifact_type, sha)
    put = await store.put_json(key, payload)
    async with session_factory() as session:
        requests = ResearchRequestRepository(session)
        await requests.mark_running(request_id)
        record = await ResearchArtifactRepository(session).record(
            request_id=request_id,
            artifact_type=artifact_type,
            schema_version="1.0",
            bucket="artifacts",
            object_key=key,
            sha256=put.sha256,
            size_bytes=put.size,
        )
        await requests.finish(
            request_id,
            ResearchStatus.COMPLETED,
            result=ResearchResult(
                request_id=request_id,
                execution_status=ResearchStatus.COMPLETED,
                artifact_refs=(
                    ResearchArtifactRef(
                        artifact_type=artifact_type, artifact_id=record.id, sha256=sha
                    ),
                ),
                coverage=ResearchCoverage(items_requested=1, items_covered=1),
            ),
        )
        await session.commit()
    return key


# ------------------------------------------------------------------ 冪等


async def test_submit_is_idempotent_by_key(session_factory, artifact_store) -> None:
    gateway = _gateway(session_factory, artifact_store)
    first = await gateway.submit(_submit("evidence:ep-1"))
    again = await gateway.submit(_submit("evidence:ep-1"))
    assert first.request.id == again.request.id
    assert first.request.status is ResearchStatus.QUEUED
    assert (first.reused, again.reused) == (False, False)
    assert await _count(session_factory) == 1


async def test_same_key_with_a_different_meaning_is_a_conflict(
    session_factory, artifact_store
) -> None:
    gateway = _gateway(session_factory, artifact_store)
    await gateway.submit(_submit("evidence:ep-1"))
    other = evidence_payload(audience="someone else")
    with pytest.raises(ResearchIdempotencyConflictError):
        await gateway.submit(_submit("evidence:ep-1", other))
    assert await _count(session_factory) == 1


async def test_unset_money_limits_are_filled_from_settings_and_frozen(
    session_factory, artifact_store
) -> None:
    config = GatewayConfig(
        provider_mode="fake",
        provider_is_real=False,
        provider_configured=True,
        default_max_cost_usd=Decimal("0.50"),
        default_max_youtube_units=300,
    )
    gateway = _gateway(session_factory, artifact_store, config)
    implicit = await gateway.submit(_submit("evidence:implicit"))
    assert implicit.request.limits["max_cost_usd"] == "0.50"
    assert implicit.request.limits["max_youtube_units"] == 300
    explicit = await gateway.submit(
        _submit("evidence:explicit", evidence_payload(limits={"max_searches": 2}))
    )
    assert explicit.request.limits["max_cost_usd"] is None  # 依頼が明示した上限は上書きしない


# ------------------------------------------------------------------ 鮮度キャッシュ


async def test_a_fresh_completed_request_with_the_same_meaning_is_reused(
    session_factory, artifact_store
) -> None:
    gateway = _gateway(session_factory, artifact_store)
    first = await gateway.submit(_submit("evidence:ep-1"))
    await _complete(session_factory, artifact_store, first.request.id)

    second = await gateway.submit(_submit("evidence:ep-2"))
    assert second.reused is True
    assert second.request.id == first.request.id
    assert await _count(session_factory) == 1  # 新しい依頼を保存していない


async def test_reuse_is_rejected_when_the_stored_object_no_longer_matches(
    session_factory, artifact_store
) -> None:
    """記録の sha256 と本体が食い違う成果物を再利用しない（base INV-31 の考え方）。"""
    gateway = _gateway(session_factory, artifact_store)
    first = await gateway.submit(_submit("evidence:ep-1"))
    key = await _complete(session_factory, artifact_store, first.request.id)
    artifact_store._objects[key] = b'{"tampered":true}'

    second = await gateway.submit(_submit("evidence:ep-2"))
    assert second.reused is False
    assert second.request.id != first.request.id
    assert second.request.status is ResearchStatus.QUEUED
    assert await _count(session_factory) == 2


async def test_reuse_is_rejected_when_the_stored_object_is_missing(
    session_factory, artifact_store
) -> None:
    gateway = _gateway(session_factory, artifact_store)
    first = await gateway.submit(_submit("evidence:ep-1"))
    key = await _complete(session_factory, artifact_store, first.request.id)
    del artifact_store._objects[key]

    second = await gateway.submit(_submit("evidence:ep-2"))
    assert second.reused is False and second.request.id != first.request.id


async def test_reuse_respects_the_freshness_window_per_kind(
    session_factory, artifact_store
) -> None:
    first = await _gateway(session_factory, artifact_store).submit(
        _submit("trend:1", trend_payload())
    )
    await _complete(
        session_factory, artifact_store, first.request.id, ResearchArtifactType.RESEARCH_TREND
    )

    within = _gateway(session_factory, artifact_store, now=datetime.now(UTC) + timedelta(hours=23))
    assert (await within.submit(_submit("trend:2", trend_payload()))).reused is True
    stale = _gateway(session_factory, artifact_store, now=datetime.now(UTC) + timedelta(hours=25))
    assert (await stale.submit(_submit("trend:3", trend_payload()))).reused is False


async def test_a_blocked_request_is_never_reused(session_factory, artifact_store) -> None:
    await _gateway(session_factory, artifact_store, NONE).submit(_submit("evidence:ep-1"))
    second = await _gateway(session_factory, artifact_store, NONE).submit(_submit("evidence:ep-2"))
    assert second.reused is False
    assert await _count(session_factory) == 2


# ------------------------------------------------------------------ 予算の fail-closed


async def test_provider_none_blocks_with_a_recorded_reason_and_no_call(
    session_factory, artifact_store
) -> None:
    result = await _gateway(session_factory, artifact_store, NONE).submit(_submit("evidence:ep-1"))
    assert result.request.status is ResearchStatus.BLOCKED
    assert result.request.blocked_reason is not None
    assert result.request.blocked_reason["code"] == "provider_not_configured"
    assert result.request.started_at is not None  # 開始した記録は残す（running を経由する）
    assert await _count(session_factory, ResearchCallRow) == 0


@pytest.mark.parametrize(
    "limits",
    [{}, {"max_cost_usd": "1.00"}, {"max_youtube_units": 500}],
)
async def test_a_real_provider_without_money_and_quota_limits_is_blocked_before_any_call(
    session_factory, artifact_store, limits: dict
) -> None:
    payload = evidence_payload(limits=limits) if limits else evidence_payload()
    result = await _gateway(session_factory, artifact_store, REAL).submit(
        _submit("evidence:ep-1", payload)
    )
    assert result.request.status is ResearchStatus.BLOCKED
    assert result.request.blocked_reason is not None
    assert result.request.blocked_reason["code"] == "budget_not_set"
    assert await _count(session_factory, ResearchCallRow) == 0


async def test_a_real_provider_with_both_limits_is_queued(session_factory, artifact_store) -> None:
    payload = evidence_payload(limits={"max_cost_usd": "1.00", "max_youtube_units": 500})
    result = await _gateway(session_factory, artifact_store, REAL).submit(
        _submit("evidence:ep-1", payload)
    )
    assert result.request.status is ResearchStatus.QUEUED


# ------------------------------------------------------------------ 再開


async def test_resume_refuses_a_request_whose_frozen_limits_still_lack_budget(
    session_factory, artifact_store
) -> None:
    gateway = _gateway(session_factory, artifact_store, REAL)
    blocked = await gateway.submit(_submit("evidence:ep-1"))
    resumed = await gateway.resume(blocked.request.id)
    assert resumed is not None
    assert resumed.resumed is False and resumed.reason == "budget_not_set"
    assert resumed.request.status is ResearchStatus.BLOCKED


async def test_resume_requeues_once_the_cause_is_fixed(session_factory, artifact_store) -> None:
    blocked = await _gateway(session_factory, artifact_store, NONE).submit(_submit("evidence:ep-1"))
    still = await _gateway(session_factory, artifact_store, NONE).resume(blocked.request.id)
    assert still is not None and still.resumed is False
    assert still.reason == "provider_not_configured"

    fixed = _gateway(session_factory, artifact_store, FAKE)
    resumed = await fixed.resume(blocked.request.id)
    assert resumed is not None and resumed.resumed is True
    assert resumed.request.status is ResearchStatus.QUEUED
    assert resumed.request.blocked_reason is None
    again = await fixed.resume(blocked.request.id)
    assert again is not None and again.resumed is False and again.reason == "not_blocked"


async def test_resume_of_an_unknown_request_is_none(session_factory, artifact_store) -> None:
    gateway = _gateway(session_factory, artifact_store)
    assert await gateway.resume("7d1c7a53-3a8e-5e0b-9e53-1c2b3d4e5f60") is None


async def test_the_submitted_payload_is_the_contract_spec(session_factory, artifact_store) -> None:
    """Gateway は冪等キーを依頼の意味に入れない（保存する payload は spec そのもの）。"""
    result = await _gateway(session_factory, artifact_store).submit(_submit("evidence:ep-1"))
    stored = parse_research_spec(result.request.payload)
    assert "idempotency_key" not in result.request.payload
    assert stored.kind.value == "evidence"
