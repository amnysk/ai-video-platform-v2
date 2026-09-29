"""最新の完了済み Trend の読み出し（ADR-0039 §4。B6 の Topic Planner が使う）。

守るもの:
- (channel_id, region, language[, format_profile]) で**最新の ``completed``** の Trend だけを返す
  （``partial`` / ``blocked`` / 別チャンネル・別地域・別言語は返さない）
- 返す本体は ArtifactStore から**読み戻して sha256 を照合**し、契約を通したもの
- 照合に失敗した・本体が無い・結果と成果物が食い違う・DB が落ちた、のどれでも ``None``
  （fail-closed = 「Trend 無し」。古い Trend に黙って戻らない）

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

from datetime import timedelta

from contracts.research import ResearchStatus
from infrastructure.db.research_repositories import ResearchRequestRepository
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from tests.support.research_trend import (
    TREND_AS_OF,
    make_trend_request,
    stored_trend,
    trend_executor,
    trend_providers,
    trend_request_payload,
)

FAKE = GatewayConfig(provider_mode="fake", provider_is_real=False, provider_configured=True)


def _gateway(session_factory, store) -> ResearchGateway:
    return ResearchGateway(session_factory=session_factory, store=store, config=FAKE)


async def _run(session_factory, store, key: str, providers=None, **overrides) -> str:
    request_id = await make_trend_request(session_factory, trend_request_payload(**overrides), key)
    await trend_executor(session_factory, store, providers).execute(request_id)
    return request_id


async def _latest(session_factory, store, **overrides):
    query = {"channel_id": "channel-1", "region": "JP", "language": "ja", **overrides}
    return await _gateway(session_factory, store).latest_trend(**query)


async def test_the_latest_completed_trend_is_returned_verified(
    session_factory, artifact_store
) -> None:
    older = await _run(session_factory, artifact_store, "trend:old")
    newer_as_of = (TREND_AS_OF + timedelta(hours=1)).isoformat()
    newer = await _run(session_factory, artifact_store, "trend:new", as_of=newer_as_of)

    found = await _latest(session_factory, artifact_store)
    assert found is not None and found.request_id == newer != older
    record, artifact = await stored_trend(session_factory, artifact_store, newer)
    assert found.artifact == artifact
    assert (found.artifact_ref.artifact_id, found.artifact_ref.sha256) == (record.id, record.sha256)
    assert found.observed_at == artifact.observed_at


async def test_only_completed_trends_of_the_same_channel_region_and_language_count(
    session_factory, artifact_store
) -> None:
    later = (TREND_AS_OF + timedelta(hours=2)).isoformat()
    completed = await _run(session_factory, artifact_store, "trend:ok")
    partial = await _run(
        session_factory,
        artifact_store,
        "trend:partial",
        providers=trend_providers(with_interpreter=False),
        as_of=later,
    )
    async with session_factory() as session:
        assert (await ResearchRequestRepository(session).get(partial)).status is (  # type: ignore[union-attr]
            ResearchStatus.PARTIAL
        )

    assert (await _latest(session_factory, artifact_store)).request_id == completed  # type: ignore[union-attr]
    assert await _latest(session_factory, artifact_store, channel_id="channel-2") is None
    assert await _latest(session_factory, artifact_store, region="US") is None
    assert await _latest(session_factory, artifact_store, language="en") is None
    assert await _latest(session_factory, artifact_store, format_profile="long") is None
    assert await _latest(session_factory, artifact_store, format_profile="shorts") is not None


async def test_a_tampered_or_missing_body_is_no_trend(session_factory, artifact_store) -> None:
    request_id = await _run(session_factory, artifact_store, "trend:tamper")
    record, _ = await stored_trend(session_factory, artifact_store, request_id)
    original = artifact_store._objects[record.object_key]

    artifact_store._objects[record.object_key] = b'{"tampered":true}'
    assert await _latest(session_factory, artifact_store) is None

    del artifact_store._objects[record.object_key]
    assert await _latest(session_factory, artifact_store) is None

    artifact_store._objects[record.object_key] = original
    assert await _latest(session_factory, artifact_store) is not None


async def test_a_newer_unverifiable_trend_does_not_fall_back_to_an_older_one(
    session_factory, artifact_store
) -> None:
    """最新が検証に失敗したら ``None``。古い Trend に黙って戻ると鮮度の判断を誤らせる。"""
    await _run(session_factory, artifact_store, "trend:older")
    later = (TREND_AS_OF + timedelta(days=1)).isoformat()
    newer = await _run(session_factory, artifact_store, "trend:newer", as_of=later)
    record, _ = await stored_trend(session_factory, artifact_store, newer)
    del artifact_store._objects[record.object_key]
    assert await _latest(session_factory, artifact_store) is None


async def test_a_result_that_disagrees_with_the_current_artifact_is_no_trend(
    session_factory, artifact_store
) -> None:
    request_id = await _run(session_factory, artifact_store, "trend:mismatch")
    async with session_factory() as session:
        from infrastructure.db.models import ResearchRequestRow

        row = await session.get(ResearchRequestRow, __import__("uuid").UUID(request_id))
        assert row is not None and row.result_summary is not None
        summary = dict(row.result_summary)
        summary["artifact_refs"] = [{**summary["artifact_refs"][0], "sha256": "b" * 64}]
        row.result_summary = summary
        await session.commit()
    assert await _latest(session_factory, artifact_store) is None


async def test_any_failure_while_reading_is_no_trend(session_factory, artifact_store) -> None:
    await _run(session_factory, artifact_store, "trend:boom")

    def broken_factory():
        raise RuntimeError("database is down")

    gateway = ResearchGateway(
        session_factory=broken_factory,  # type: ignore[arg-type]
        store=artifact_store,
        config=FAKE,
    )
    assert await gateway.latest_trend(channel_id="channel-1", region="JP", language="ja") is None


async def test_no_trend_at_all_is_none(session_factory, artifact_store) -> None:
    assert await _latest(session_factory, artifact_store) is None
