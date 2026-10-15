"""版付き Agent Registry の契約（ADR-0043 §2、INV-51）。

理由: docs/testing/jpmas2-p0a-rationale.md
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contracts.agents import (
    AGENT_SPECS,
    MODEL_POLICIES,
    AgentHandlerRef,
    AgentTool,
    UnknownAgentSpecError,
    UnknownModelPolicyError,
    agent_ref,
    parse_agent_ref,
    resolve_agent_spec,
    resolve_model_policy,
)
from contracts.artifact_registry import ARTIFACT_REGISTRY
from contracts.intervention import AgentRole
from contracts.roles import ROLE_SPECS
from contracts.states import ArtifactType

NEW_ROLES = (
    AgentRole.STRATEGY,
    AgentRole.CREATIVE_DIRECTOR,
    AgentRole.QUALITY_REVIEWER,
    AgentRole.LEARNING,
)

#: 登録済み spec の manifest_hash の固定値。**この値が変わるなら spec を書き換えず新しい
#: spec_version を足す**（実行中の計画は旧 version の hash を固定している。INV-51）。
PINNED_HASHES = {
    (AgentRole.STRATEGY, "1.0"): "84bce365d3528afa248e1c60054f8f1e9b34dd6de17d4962378999964f4e4f82",
    (
        AgentRole.CREATIVE_DIRECTOR,
        "1.0",
    ): "b87244c386e3388848e00d701131366456b3215fb34ec2f1912e9c6a3cbf0131",
    (
        AgentRole.QUALITY_REVIEWER,
        "1.0",
    ): "4ada80c5ef4419f782c2280f84821bd5573b9886ddefe8d97e24255ca006eb69",
    (AgentRole.LEARNING, "1.0"): "d55555ef38631ca6065d38a68e4aa1d6b59dfab2413946d8461ea3dbcf11984e",
}


def test_registered_spec_hashes_are_pinned() -> None:
    assert {k: s.manifest_hash for k, s in AGENT_SPECS.items()} == PINNED_HASHES


def test_changing_any_field_changes_the_hash() -> None:
    spec = AGENT_SPECS[(AgentRole.STRATEGY, "1.0")]
    assert replace(spec, max_llm_calls=3).manifest_hash != spec.manifest_hash
    assert replace(spec, max_estimated_cost_usd=Decimal("0.06")).manifest_hash != spec.manifest_hash
    assert replace(spec, prompt_version="strategy-v2").manifest_hash != spec.manifest_hash
    assert replace(spec).manifest_hash == spec.manifest_hash


def test_every_new_role_has_a_v1_spec_and_a_role_spec() -> None:
    for role in NEW_ROLES:
        assert resolve_agent_spec(role, "1.0").role is role
        assert role in ROLE_SPECS


def test_unknown_role_or_version_is_rejected() -> None:
    with pytest.raises(UnknownAgentSpecError):
        resolve_agent_spec("strategy", "9.9")
    with pytest.raises(UnknownAgentSpecError):
        resolve_agent_spec("nobody", "1.0")
    with pytest.raises(UnknownAgentSpecError):
        resolve_agent_spec(AgentRole.PRODUCER, "1.0")  # 既存3役は Registry の対象外
    with pytest.raises(UnknownAgentSpecError):
        parse_agent_ref("strategy")
    assert parse_agent_ref(agent_ref(AgentRole.LEARNING, "1.0")) == (AgentRole.LEARNING, "1.0")


def test_tools_handlers_and_policies_are_registered_vocabulary() -> None:
    handlers = {h.value for h in AgentHandlerRef}
    for spec in AGENT_SPECS.values():
        assert set(spec.allowed_tools) <= set(AgentTool)
        assert spec.handler_ref.value in handlers
        assert resolve_model_policy(spec.model_policy_ref) in MODEL_POLICIES.values()
        assert spec.max_llm_calls >= 1 and spec.max_tokens > 0
        assert spec.max_estimated_cost_usd > 0 and spec.timeout_seconds > 0
    assert {s.handler_ref for s in AGENT_SPECS.values()} == set(AgentHandlerRef)


def test_role_spec_tools_equal_the_agent_spec_tools() -> None:
    for role in NEW_ROLES:
        assert set(ROLE_SPECS[role].allowed_tools) == {
            t.value for t in resolve_agent_spec(role, "1.0").allowed_tools
        }
        assert (
            ROLE_SPECS[role].max_llm_calls_per_run == resolve_agent_spec(role, "1.0").max_llm_calls
        )
        assert (
            ROLE_SPECS[role].may_create_revisions
            == resolve_agent_spec(role, "1.0").may_create_revisions
        )
    assert ROLE_SPECS[AgentRole.QUALITY_REVIEWER].may_create_revisions is False
    assert ROLE_SPECS[AgentRole.LEARNING].may_create_revisions is False


def test_schema_refs_resolve_in_the_artifact_registry() -> None:
    for spec in AGENT_SPECS.values():
        for ref in (spec.input_schema_ref, spec.output_schema_ref):
            name, _, version = ref.partition("@")
            if version:
                assert (name, version) in ARTIFACT_REGISTRY, ref
            else:
                # upload_receipt は既存の成果物で版付き registry の対象外（contracts/upload.py）。
                # 版なしの参照は ArtifactType の語彙に限る
                assert ArtifactType(name) is ArtifactType.UPLOAD_RECEIPT


def test_model_policies_never_invent_a_price() -> None:
    fake = resolve_model_policy("fake_llm@1")
    assert (fake.provider, fake.model) == ("fake", "fake-deterministic")
    assert fake.input_usd_per_1k_tokens == Decimal("0") == fake.output_usd_per_1k_tokens
    codex = resolve_model_policy("codex_cli_text@1")
    assert (codex.provider, codex.model) == ("codex_cli", "configured")
    assert codex.input_usd_per_1k_tokens is None and codex.output_usd_per_1k_tokens is None
    with pytest.raises(UnknownModelPolicyError):
        resolve_model_policy("gpt@1")
