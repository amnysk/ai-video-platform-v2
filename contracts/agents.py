"""版付き Agent Registry（ADR-0043 §2、JP-MAS-002 v1.1 §2.5）。

- 役割名の語彙は ``contracts.intervention.AgentRole`` が唯一の宣言元（ここで文字列を書き直さない）。
- ``AgentSpec`` は不変。内容を変えるときは**新しい spec_version**を足す（登録済み spec の
  ``manifest_hash`` は ``tests/contract/test_agent_registry.py`` が literal で固定している）。
- handler はコード登録だけ（``AgentHandlerRef``）。import 文字列・plugin の取得はしない。
- 単価の無い ModelPolicy は費用を測れない: 実績は ``None``（0 にしない）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from contracts.intervention import AgentRole


class AgentTool(StrEnum):
    LLM_GENERATE_JSON = "llm:generate_json"
    ARTIFACT_READ = "artifact:read"
    ANALYTICS_READ = "analytics:read"
    INTERVENTION_PROPOSE = "intervention:propose"


class AgentHandlerRef(StrEnum):
    """コード登録された handler（``workers/agents/handlers.py::HANDLERS`` のキーと一致させる）。"""

    STRATEGY_AGENT_V1 = "strategy_agent_v1"
    CREATIVE_DIRECTOR_V1 = "creative_director_v1"
    QUALITY_REVIEWER_V1 = "quality_reviewer_v1"
    LEARNING_AGENT_V1 = "learning_agent_v1"


class UnknownAgentSpecError(LookupError):
    pass


class UnknownModelPolicyError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    policy_id: str
    version: int
    provider: Literal["fake", "codex_cli"]
    model: str
    #: None = 単価が無い（費用を測れない。0 ではない）
    input_usd_per_1k_tokens: Decimal | None = None
    output_usd_per_1k_tokens: Decimal | None = None

    @property
    def ref(self) -> str:
        return f"{self.policy_id}@{self.version}"


MODEL_POLICIES: dict[str, ModelPolicy] = {
    p.ref: p
    for p in (
        ModelPolicy("fake_llm", 1, "fake", "fake-deterministic", Decimal("0"), Decimal("0")),
        ModelPolicy("codex_cli_text", 1, "codex_cli", "configured", None, None),
    )
}


@dataclass(frozen=True, slots=True)
class AgentSpec:
    role: AgentRole
    spec_version: str
    input_schema_ref: str
    output_schema_ref: str
    handler_ref: AgentHandlerRef
    allowed_tools: tuple[AgentTool, ...]
    model_policy_ref: str
    prompt_version: str
    max_llm_calls: int
    max_tokens: int
    max_estimated_cost_usd: Decimal
    timeout_seconds: int
    retry_max_attempts: int
    termination: tuple[str, ...]
    may_create_revisions: bool

    def canonical(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "spec_version": self.spec_version,
            "input_schema_ref": self.input_schema_ref,
            "output_schema_ref": self.output_schema_ref,
            "handler_ref": self.handler_ref.value,
            "allowed_tools": [t.value for t in self.allowed_tools],
            "model_policy_ref": self.model_policy_ref,
            "prompt_version": self.prompt_version,
            "max_llm_calls": self.max_llm_calls,
            "max_tokens": self.max_tokens,
            "max_estimated_cost_usd": str(self.max_estimated_cost_usd),
            "timeout_seconds": self.timeout_seconds,
            "retry_max_attempts": self.retry_max_attempts,
            "termination": list(self.termination),
            "may_create_revisions": self.may_create_revisions,
        }

    @property
    def manifest_hash(self) -> str:
        text = json.dumps(
            self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


_T = AgentTool


def _spec(
    role: AgentRole,
    input_ref: str,
    output_ref: str,
    handler: AgentHandlerRef,
    tools: tuple[AgentTool, ...],
    max_llm_calls: int,
    max_cost: str,
    prompt_version: str,
    termination: tuple[str, ...],
    may_create_revisions: bool,
) -> AgentSpec:
    return AgentSpec(
        role=role,
        spec_version="1.0",
        input_schema_ref=input_ref,
        output_schema_ref=output_ref,
        handler_ref=handler,
        allowed_tools=tools,
        model_policy_ref="codex_cli_text@1",
        prompt_version=prompt_version,
        max_llm_calls=max_llm_calls,
        max_tokens=8000,
        max_estimated_cost_usd=Decimal(max_cost),
        timeout_seconds=300,
        retry_max_attempts=3,
        termination=termination,
        may_create_revisions=may_create_revisions,
    )


#: 終了条件の文言の正本。``contracts/roles.py`` の RoleSpec も同じものを使う（再掲しない）。
TERMINATION: dict[AgentRole, tuple[str, ...]] = {
    AgentRole.STRATEGY: (
        "契約を通った StrategyBrief を保存した",
        "LLM の形式不正が retry 上限に達した（blocked）",
    ),
    AgentRole.CREATIVE_DIRECTOR: (
        "契約を通った CreativeBrief を保存した",
        "LLM の形式不正が retry 上限に達した（blocked）",
    ),
    AgentRole.QUALITY_REVIEWER: (
        "契約を通った QualityReview を保存した",
        "入力が欠けるとき blocked_missing_input の QualityReview を保存した",
    ),
    AgentRole.LEARNING: (
        "測定が揃い Lesson(status=proposed) を保存した",
        "private のまま・測定欠測・小標本なら awaiting_public_metrics で Lesson を作らず終了した",
    ),
}

_SPECS = (
    _spec(
        AgentRole.STRATEGY,
        "user_brief@1.0",
        "strategy_brief@1.0",
        AgentHandlerRef.STRATEGY_AGENT_V1,
        (_T.LLM_GENERATE_JSON, _T.ARTIFACT_READ, _T.ANALYTICS_READ),
        2,
        "0.05",
        "strategy-v1",
        TERMINATION[AgentRole.STRATEGY],
        True,
    ),
    _spec(
        AgentRole.CREATIVE_DIRECTOR,
        "strategy_brief@1.0",
        "creative_brief@1.0",
        AgentHandlerRef.CREATIVE_DIRECTOR_V1,
        (_T.LLM_GENERATE_JSON, _T.ARTIFACT_READ, _T.INTERVENTION_PROPOSE),
        2,
        "0.05",
        "creative-v1",
        TERMINATION[AgentRole.CREATIVE_DIRECTOR],
        True,
    ),
    _spec(
        AgentRole.QUALITY_REVIEWER,
        "storyboard@1.0",
        "quality_review@1.0",
        AgentHandlerRef.QUALITY_REVIEWER_V1,
        (_T.LLM_GENERATE_JSON, _T.ARTIFACT_READ, _T.INTERVENTION_PROPOSE),
        2,
        "0.05",
        "quality-v1",
        TERMINATION[AgentRole.QUALITY_REVIEWER],
        False,
    ),
    _spec(
        AgentRole.LEARNING,
        "upload_receipt",
        "lesson@1.0",
        AgentHandlerRef.LEARNING_AGENT_V1,
        (_T.LLM_GENERATE_JSON, _T.ARTIFACT_READ, _T.ANALYTICS_READ),
        1,
        "0.03",
        "learning-v1",
        TERMINATION[AgentRole.LEARNING],
        False,
    ),
)

AGENT_SPECS: dict[tuple[AgentRole, str], AgentSpec] = {(s.role, s.spec_version): s for s in _SPECS}


def resolve_agent_spec(
    role: AgentRole | str,
    version: str,
    *,
    specs: dict[tuple[AgentRole, str], AgentSpec] = AGENT_SPECS,
) -> AgentSpec:
    try:
        key = (AgentRole(role), version)
    except ValueError as exc:
        raise UnknownAgentSpecError(f"unknown agent role: {role!r}") from exc
    spec = specs.get(key)
    if spec is None:
        raise UnknownAgentSpecError(f"unknown agent spec: {key[0].value}@{version}")
    return spec


def resolve_model_policy(
    ref: str, *, policies: dict[str, ModelPolicy] = MODEL_POLICIES
) -> ModelPolicy:
    policy = policies.get(ref)
    if policy is None:
        raise UnknownModelPolicyError(f"unknown model policy: {ref!r}")
    return policy


def agent_ref(role: AgentRole | str, version: str) -> str:
    return f"{AgentRole(role).value}@{version}"


def parse_agent_ref(ref: str) -> tuple[AgentRole, str]:
    role, sep, version = ref.partition("@")
    if not sep or not version:
        raise UnknownAgentSpecError(f"agent ref must be 'role@version': {ref!r}")
    try:
        return AgentRole(role), version
    except ValueError as exc:
        raise UnknownAgentSpecError(f"unknown agent role: {role!r}") from exc
