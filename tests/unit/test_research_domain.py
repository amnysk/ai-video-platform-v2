"""Research の純粋なドメイン（ADR-0037）: id・状態機械・呼び出し台帳の状態・request_hash・キー。

理由は docs/testing/research-persistence-rationale.md。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from contracts.research import (
    ResearchArtifactType,
    ResearchCallStatus,
    ResearchStatus,
    parse_research_spec,
)
from domain.episode.transitions import Rejected
from domain.research.calls import CallEvent, transition_call
from domain.research.identity import request_hash, request_hash_payload
from domain.research.ids import RESEARCH_NAMESPACE, research_request_id_for
from domain.research.keys import RESEARCH_KEY_PREFIX, research_artifact_object_key
from domain.research.status import (
    RESEARCH_TERMINAL_STATUSES,
    RESEARCH_TRANSITIONS,
    ResearchEvent,
    transition_research,
)
from tests.support.research import evidence_payload, trend_payload

# ------------------------------------------------------------------ ids


def test_request_id_is_derived_from_the_idempotency_key() -> None:
    first = research_request_id_for("trend:channel-1:2026-09-29")
    assert first == research_request_id_for("trend:channel-1:2026-09-29")
    assert first != research_request_id_for("trend:channel-1:2026-09-30")
    assert first == str(uuid.uuid5(RESEARCH_NAMESPACE, "trend:channel-1:2026-09-29"))
    with pytest.raises(ValueError):
        research_request_id_for("")


# ------------------------------------------------------------------ 依頼の状態機械


def test_research_status_table() -> None:
    assert transition_research(ResearchStatus.QUEUED, ResearchEvent.STARTED) is (
        ResearchStatus.RUNNING
    )
    for event, target in (
        (ResearchEvent.COMPLETED, ResearchStatus.COMPLETED),
        (ResearchEvent.PARTIAL, ResearchStatus.PARTIAL),
        (ResearchEvent.BLOCKED, ResearchStatus.BLOCKED),
        (ResearchEvent.FAILED, ResearchStatus.FAILED),
    ):
        assert transition_research(ResearchStatus.RUNNING, event) is target
    assert transition_research(ResearchStatus.BLOCKED, ResearchEvent.RESUMED) is (
        ResearchStatus.QUEUED
    )


def test_terminal_statuses_have_no_outgoing_edges() -> None:
    assert {
        ResearchStatus.COMPLETED,
        ResearchStatus.PARTIAL,
        ResearchStatus.FAILED,
    } == RESEARCH_TERMINAL_STATUSES
    assert not [key for key in RESEARCH_TRANSITIONS if key[0] in RESEARCH_TERMINAL_STATUSES]
    rejected = transition_research(ResearchStatus.COMPLETED, ResearchEvent.STARTED)
    assert isinstance(rejected, Rejected)


def test_a_queued_request_cannot_finish_without_running() -> None:
    assert isinstance(transition_research(ResearchStatus.QUEUED, ResearchEvent.COMPLETED), Rejected)


# ------------------------------------------------------------------ 呼び出し台帳の状態


def test_call_status_table_only_leaves_reserved() -> None:
    assert transition_call(ResearchCallStatus.RESERVED, CallEvent.SPENT, dispatched=True) is (
        ResearchCallStatus.SPENT
    )
    assert transition_call(ResearchCallStatus.RESERVED, CallEvent.SPENT, dispatched=False) is (
        ResearchCallStatus.SPENT
    )
    assert transition_call(ResearchCallStatus.RESERVED, CallEvent.ABANDONED, dispatched=False) is (
        ResearchCallStatus.ABANDONED
    )
    for status in (ResearchCallStatus.SPENT, ResearchCallStatus.ABANDONED):
        for event in CallEvent:
            assert isinstance(transition_call(status, event, dispatched=False), Rejected)


def test_a_dispatched_call_can_never_be_abandoned() -> None:
    """送ったかもしれない呼び出しを「送っていない」にしない（INV-15 の保守的な扱い）。"""
    result = transition_call(ResearchCallStatus.RESERVED, CallEvent.ABANDONED, dispatched=True)
    assert isinstance(result, Rejected)


# ------------------------------------------------------------------ request_hash


def _hash(payload: dict[str, Any], provider_config_version: str = "provider-config-1") -> str:
    return request_hash(
        parse_research_spec(payload), provider_config_version=provider_config_version
    )


def test_request_hash_ignores_who_asked_and_the_episode_reference() -> None:
    base = _hash(evidence_payload())
    assert base == _hash(evidence_payload(requester="api"))
    assert base == _hash(evidence_payload(episode_id=str(uuid.uuid4())))


def test_request_hash_normalizes_claim_text_and_order() -> None:
    claims = [
        {"claim_text": "A opened in 1883", "kind": "year", "importance": "central"},
        {"claim_text": "B is the tallest", "kind": "superlative", "importance": "supporting"},
    ]
    shuffled = [
        {"claim_text": "b  IS the tallest", "kind": "superlative", "importance": "supporting"},
        {"claim_text": "A opened in 1883", "kind": "year", "importance": "central"},
    ]
    assert _hash(evidence_payload(inputs={"claim_inputs": claims})) == _hash(
        evidence_payload(inputs={"claim_inputs": shuffled})
    )


def test_request_hash_changes_with_meaning_limits_and_provider_config() -> None:
    base = _hash(trend_payload())
    assert base != _hash(trend_payload(channel_id="channel-2"))
    assert base != _hash(trend_payload(limits={"max_searches": 2}))
    assert base != _hash(trend_payload(), provider_config_version="provider-config-2")


def test_trend_hash_rounds_as_of_to_the_utc_day_but_evidence_ignores_as_of() -> None:
    later = datetime(2026, 9, 29, 23, 0, tzinfo=UTC).isoformat()
    next_day = datetime(2026, 9, 30, 1, 0, tzinfo=UTC).isoformat()
    assert _hash(trend_payload()) == _hash(trend_payload(as_of=later))
    assert _hash(trend_payload()) != _hash(trend_payload(as_of=next_day))
    assert _hash(evidence_payload()) == _hash(evidence_payload(as_of=next_day))


def test_equal_money_limits_hash_equally() -> None:
    assert _hash(trend_payload(limits={"max_cost_usd": "1.50"})) == _hash(
        trend_payload(limits={"max_cost_usd": "1.5"})
    )


def test_request_hash_payload_is_json_ready() -> None:
    import json

    spec = parse_research_spec(trend_payload())
    payload = request_hash_payload(spec, provider_config_version="provider-config-1")
    json.dumps(payload)  # datetime/Decimal が残っていないこと
    assert "requester" not in payload and "episode_id" not in payload


# ------------------------------------------------------------------ オブジェクトキー


def test_research_objects_live_under_their_own_prefix() -> None:
    request_id = str(uuid.uuid4())
    key = research_artifact_object_key(request_id, ResearchArtifactType.RESEARCH_TREND, "a" * 64)
    assert key == f"{RESEARCH_KEY_PREFIX}/{request_id}/research_trend/{'a' * 64}.json"
    assert not key.startswith("artifacts/")
    with pytest.raises(ValueError):
        research_artifact_object_key("not-a-uuid", ResearchArtifactType.RESEARCH_TREND, "a" * 64)
    with pytest.raises(ValueError):
        research_artifact_object_key(request_id, ResearchArtifactType.RESEARCH_TREND, "A" * 64)
