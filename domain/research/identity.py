"""Research 依頼の同一性（``request_hash``。ADR-0037 §2）。純粋関数のみ。

``request_hash`` は「同じ**意味**の依頼か」の判定に使う（同じ hash で完了済み・鮮度内の依頼が
あれば再利用する）。構成要素の定義は**ここに 1 つだけ**置く（AGENTS.md §8）。

含める: ``kind`` / claim inputs または Trend の入力（正規化後）/ ``channel_id`` /
audience・language・format_profile / ``time_window``（絶対日時）/ ``input_artifact_refs`` の
sha256 / ``policy_version`` / ``prompt_version`` / ``schema_version`` /
``provider_config_version`` / ``limits``。Trend は ``as_of`` を **UTC 日付**に丸めて含める
（古い結果への永久固定を防ぐ）。

含めない: Temporal attempt・workflow run id・作成時刻・``requester``・``idempotency_key``
（誰が・いつ・何回目に頼んだかは意味ではない）。``episode_id`` も含めない（参照であって
所有ではなく、Evidence は複数 Episode で共有する）。Evidence の ``as_of`` も含めない（意味は
``time_window`` が決める）。``input_artifact_refs`` は ``artifact_id`` ではなく **sha256** だけ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from contracts.research import (
    RESEARCH_PROMPT_VERSION,
    EvidenceResearchRequest,
    ResearchLimits,
    TimeWindow,
    TrendResearchRequest,
    normalize_claim_text,
    refresh_slot_of,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex

__all__ = ["request_hash", "request_hash_payload"]


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _window(window: TimeWindow) -> dict[str, str]:
    return {"start": _utc(window.start), "end": _utc(window.end)}


def _decimal(value: Decimal | None) -> str | None:
    """``Decimal('1.50')`` と ``Decimal('1.5')`` を同じ文字列にする（同じ金額は同じ hash）。"""
    return None if value is None else format(value.normalize(), "f")


def _limits(limits: ResearchLimits) -> dict[str, Any]:
    return {
        "max_searches": limits.max_searches,
        "max_fetches": limits.max_fetches,
        "max_assessments": limits.max_assessments,
        "max_followup_rounds": limits.max_followup_rounds,
        "max_youtube_units": limits.max_youtube_units,
        "max_cost_usd": _decimal(limits.max_cost_usd),
        "deadline_seconds": limits.deadline_seconds,
    }


def request_hash_payload(
    spec: TrendResearchRequest | EvidenceResearchRequest,
    *,
    provider_config_version: str,
    prompt_version: str = RESEARCH_PROMPT_VERSION,
) -> dict[str, Any]:
    """hash の材料（正準化前の dict）。テストと調査のために公開する。"""
    payload: dict[str, Any] = {
        "kind": spec.kind.value,
        "channel_id": spec.channel_id,
        "audience": normalize_claim_text(spec.audience),
        "language": spec.language,
        "format_profile": spec.format_profile,
        "time_window": _window(spec.time_window),
        "input_artifact_sha256": sorted(ref.sha256 for ref in spec.input_artifact_refs),
        "policy_version": spec.policy_version,
        "prompt_version": prompt_version,
        "schema_version": spec.schema_version,
        "provider_config_version": provider_config_version,
        "limits": _limits(spec.limits),
    }
    if isinstance(spec, TrendResearchRequest):
        inputs = spec.inputs
        payload["inputs"] = {
            "region": inputs.region,
            "audience_hypothesis": normalize_claim_text(inputs.audience_hypothesis),
            "seed_terms": sorted(normalize_claim_text(t) for t in inputs.seed_terms),
            "past_video_refs": sorted(inputs.past_video_refs),
            "analytics_ref": inputs.analytics_ref,
        }
        payload["refresh_slot"] = refresh_slot_of(spec.as_of)
    else:
        payload["inputs"] = {
            "claims": sorted(
                (
                    {
                        "text": normalize_claim_text(c.claim_text),
                        "kind": c.kind.value,
                        "era": None if c.era is None else normalize_claim_text(c.era),
                        "region": None if c.region is None else normalize_claim_text(c.region),
                        "importance": c.importance.value,
                    }
                    for c in spec.inputs.claim_inputs
                ),
                key=lambda claim: claim["text"],
            )
        }
    return payload


def request_hash(
    spec: TrendResearchRequest | EvidenceResearchRequest,
    *,
    provider_config_version: str,
    prompt_version: str = RESEARCH_PROMPT_VERSION,
) -> str:
    """依頼の意味の指紋（sha256 hex）。"""
    payload = request_hash_payload(
        spec, provider_config_version=provider_config_version, prompt_version=prompt_version
    )
    return sha256_hex(canonical_json_bytes(payload))
