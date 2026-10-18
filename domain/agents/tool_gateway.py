"""Tool Gateway（ADR-0043 §2、INV-56）。

handler は gateway 経由でしか外部を呼べない。

- ``spec.allowed_tools`` に無い tool、または供給されていない tool は、**何かを呼ぶ前**に
  ``ToolNotAllowedError``
- LLM 呼び出しは ``spec.max_llm_calls`` まで。超過は呼ぶ前に ``AgentBudgetExceededError``
- 呼び出しごとに記録を残す（tool・成否・token・費用。**prompt 本文は残さない**）
- 費用は token usage × policy 単価。欠測は ``None``（0 にしない）

Agent Tool（ADR-0047）: ``call_tool`` は ``spec.allowed_tool_ids`` に無い tool・入力 schema 違反・
``max_calls_per_run`` 超過を**外部を呼ぶ前**に拒否する。実行は ``tool_call_id`` ごとに
``ToolCallStore`` へ記録し、保存済みの結果は executor を呼ばずに返す（INV-59）。結果は全て
``ToolEnvelope``（untrusted）。``tool_runtime`` が無い（``JPMAS2_AGENT_TOOLS`` 無効）gateway は
従来どおりで、``call_tool`` は全て拒否する。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from contracts.agent_tools import (
    ToolDef,
    ToolEnvelope,
    ToolId,
    ToolStatus,
    input_sha256,
    parse_tool_ref,
    tool_call_id,
)
from contracts.agents import AgentSpec, AgentTool, ModelPolicy
from domain.agents.cost import llm_call_cost, sum_costs
from domain.agents.data_block import data_block
from domain.agents.ports import AgentLlm
from domain.agents.tools.observer import NoopToolObserver, ToolEvent, ToolObserver
from domain.agents.tools.ports import ToolContext, ToolExecutor, ToolOutcome
from domain.agents.tools.store import ToolCallCompletion, ToolCallStart, ToolCallStore
from domain.errors import (
    AgentBudgetExceededError,
    ToolCallLimitExceededError,
    ToolInputInvalidError,
    ToolNotAllowedError,
)

ToolFn = Callable[..., Awaitable[Any]]

#: 呼んだか分からない（``started`` のまま結果が無い）行を再送しない（youtube_quota。INV-59）
ERROR_AMBIGUOUS_PRIOR_ATTEMPT = "ambiguous_prior_attempt"
ERROR_OUTPUT_INVALID = "output_validation_failed"
ERROR_ENVELOPE_INVALID = "envelope_invalid"
ERROR_EXECUTOR_CONTRACT = "executor_contract_violation"

REFUSED_NOT_ALLOWED = "tool_not_allowed"
REFUSED_UNKNOWN = "unknown_tool"
REFUSED_NOT_PROVIDED = "tool_not_provided"
REFUSED_INVALID_INPUT = "invalid_tool_input"
REFUSED_MAX_CALLS = "max_calls_exceeded"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ToolRuntime:
    """Agent Tool の実行に要るもの。``None`` の gateway は tool を一切実行しない。"""

    #: ``"tool_id@version"`` -> 定義（契約の ``TOOL_DEFS``）
    defs: Mapping[str, ToolDef]
    #: 有効（``JPMAS2_ENABLED_TOOLS``）かつ供給された executor だけ
    executors: Mapping[ToolId, ToolExecutor]
    store: ToolCallStore
    episode_id: str
    #: 冪等キー（``agent_runs.idempotency_key``）
    run_key: str
    observer: ToolObserver = field(default_factory=NoopToolObserver)
    clock: Callable[[], datetime] = _utcnow


@dataclass(frozen=True)
class ToolSupport:
    """run に依らない tool の供給（worker が 1 度組み立てる）。flag 無効なら作らない。"""

    defs: Mapping[str, ToolDef]
    executors: Mapping[ToolId, ToolExecutor]
    store: ToolCallStore
    observer: ToolObserver = field(default_factory=NoopToolObserver)

    def runtime(
        self, *, episode_id: str, run_key: str, clock: Callable[[], datetime] = _utcnow
    ) -> ToolRuntime:
        return ToolRuntime(
            defs=self.defs,
            executors=self.executors,
            store=self.store,
            episode_id=episode_id,
            run_key=run_key,
            observer=self.observer,
            clock=clock,
        )


class ToolGateway:
    def __init__(
        self,
        spec: AgentSpec,
        policy: ModelPolicy,
        llm: AgentLlm,
        tools: Mapping[AgentTool, ToolFn],
        tool_runtime: ToolRuntime | None = None,
    ) -> None:
        self._runtime = tool_runtime
        self._tool_seq = 0
        self._tool_calls: dict[str, int] = {}
        self._spec = spec
        self._policy = policy
        self._llm = llm
        self._tools = dict(tools)
        self._records: list[dict[str, Any]] = []
        self._llm_calls = 0
        #: LLM を実際に呼んだ回数ぶんの費用（None = 不明）。拒否した呼び出しは含めない
        self._llm_costs: list[Decimal | None] = []
        self._llm_input: list[int | None] = []
        self._llm_output: list[int | None] = []

    # --- 呼び出し -----------------------------------------------------------

    async def llm_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        tool = AgentTool.LLM_GENERATE_JSON
        self._require_allowed(tool)
        if self._llm_calls >= self._spec.max_llm_calls:
            self._refuse(tool, "AgentBudgetExceededError")
            raise AgentBudgetExceededError(
                f"{self._spec.role.value}@{self._spec.spec_version} may call the LLM at most "
                f"{self._spec.max_llm_calls} times"
            )
        self._llm_calls += 1
        try:
            result = await self._llm.generate_json(
                prompt=prompt, schema=schema, max_tokens=self._spec.max_tokens
            )
        except Exception as exc:
            # 呼んだ上で失敗した: 使用量・費用は不明（0 にしない）
            self._llm_costs.append(None)
            self._llm_input.append(None)
            self._llm_output.append(None)
            self._records.append(
                {
                    "tool": tool.value,
                    "ok": False,
                    "error": type(exc).__name__,
                    "model": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_usd": None,
                }
            )
            raise
        cost = llm_call_cost(self._policy, result.input_tokens, result.output_tokens)
        self._llm_costs.append(cost)
        self._llm_input.append(result.input_tokens)
        self._llm_output.append(result.output_tokens)
        self._records.append(
            {
                "tool": tool.value,
                "ok": True,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": None if cost is None else str(cost),
            }
        )
        return result.data

    async def call(self, tool: AgentTool, **kwargs: Any) -> Any:
        """LLM 以外の tool。LLM は ``llm_json`` を使う（回数上限と費用の記録のため）。"""
        if tool is AgentTool.LLM_GENERATE_JSON:
            self._refuse(tool, "ToolNotAllowedError")
            raise ToolNotAllowedError("use llm_json() for llm:generate_json")
        self._require_allowed(tool)
        try:
            result = await self._tools[tool](**kwargs)
        except Exception as exc:
            self._records.append({"tool": tool.value, "ok": False, "error": type(exc).__name__})
            raise
        self._records.append({"tool": tool.value, "ok": True})
        return result

    # --- Agent Tool（ADR-0047） --------------------------------------------

    async def call_tool(self, tool_ref: str, tool_input: Mapping[str, Any]) -> ToolEnvelope:
        """許可・入力・上限を検査し、保存済みなら再利用して、結果を ``ToolEnvelope`` で返す。"""
        definition = self._admit_tool(tool_ref)
        runtime = self._runtime
        assert runtime is not None  # _admit_tool が保証
        try:
            parsed = definition.input_model.model_validate(dict(tool_input))
        except ValidationError:
            self._refuse_tool(tool_ref, definition, REFUSED_INVALID_INPUT)
            raise ToolInputInvalidError(f"invalid input for {tool_ref}") from None
        normalized = parsed.model_dump(mode="json")
        if self._tool_calls.get(definition.ref, 0) >= definition.max_calls_per_run:
            self._refuse_tool(tool_ref, definition, REFUSED_MAX_CALLS)
            raise ToolCallLimitExceededError(
                f"{definition.ref} may be called at most "
                f"{definition.max_calls_per_run} times per run"
            )
        self._tool_calls[definition.ref] = self._tool_calls.get(definition.ref, 0) + 1
        self._tool_seq += 1
        seq = self._tool_seq
        sha = input_sha256(normalized)
        call_id = tool_call_id(runtime.run_key, seq, definition.tool_id, sha)
        started_at = runtime.clock()
        t0 = time.monotonic()

        stored = await runtime.store.get(call_id)
        cache_hit = False
        research_request_id: str | None = None
        if stored is not None and stored.envelope is not None:
            # 保存済み: executor を呼ばない（INV-59）
            self._finish_tool(
                definition, stored.envelope, seq, t0, cache_hit=True, error_code=stored.error_code
            )
            return stored.envelope
        if stored is not None:
            # ``started`` のまま結果が無い: 呼んだか分からない
            if definition.cost_class == "youtube_quota":
                envelope = self._envelope(
                    call_id,
                    definition,
                    ToolOutcome(status=ToolStatus.FAILED, error_code=ERROR_AMBIGUOUS_PRIOR_ATTEMPT),
                )
                await runtime.store.complete(
                    call_id,
                    ToolCallCompletion(
                        envelope=envelope,
                        finished_at=runtime.clock(),
                        duration_ms=_ms(t0),
                        error_code=ERROR_AMBIGUOUS_PRIOR_ATTEMPT,
                    ),
                )
                self._finish_tool(
                    definition,
                    envelope,
                    seq,
                    t0,
                    cache_hit=False,
                    error_code=ERROR_AMBIGUOUS_PRIOR_ATTEMPT,
                )
                return envelope
            research_request_id = stored.research_request_id
        else:
            await runtime.store.begin(
                ToolCallStart(
                    tool_call_id=call_id,
                    run_key=runtime.run_key,
                    episode_id=runtime.episode_id,
                    agent_role=self._spec.role.value,
                    agent_spec_version=self._spec.spec_version,
                    seq=seq,
                    tool_id=definition.tool_id.value,
                    tool_version=definition.version,
                    input_sha256=sha,
                    tool_input=normalized,
                    started_at=started_at,
                )
            )
        outcome = await self._execute(
            definition,
            ToolContext(
                tool_call_id=call_id,
                episode_id=runtime.episode_id,
                agent_ref=f"{self._spec.role.value}@{self._spec.spec_version}",
                run_key=runtime.run_key,
                seq=seq,
                idempotency_key=f"tool:{call_id}",
                now=started_at,
            ),
            parsed,
        )
        envelope = self._envelope(call_id, definition, outcome)
        error_code = outcome.error_code
        if error_code is None and envelope.status is ToolStatus.FAILED and envelope.limitations:
            error_code = envelope.limitations[0]
        await runtime.store.complete(
            call_id,
            ToolCallCompletion(
                envelope=envelope,
                finished_at=runtime.clock(),
                duration_ms=_ms(t0),
                research_request_id=outcome.research_request_id or research_request_id,
                error_code=error_code,
            ),
        )
        self._finish_tool(definition, envelope, seq, t0, cache_hit=cache_hit, error_code=error_code)
        return envelope

    @property
    def has_tool_runtime(self) -> bool:
        """``JPMAS2_AGENT_TOOLS`` が有効で tool を実行できる gateway か（無効は全て拒否）。"""
        return self._runtime is not None

    def available_tools(self) -> tuple[ToolDef, ...]:
        """この run で実際に呼べる tool（spec が許可・登録済み・executor が供給済み）。

        handler が plan の候補を作るための参照で、実行の可否は ``call_tool`` が改めて検査する。
        """
        runtime = self._runtime
        if runtime is None:
            return ()
        found: list[ToolDef] = []
        for ref in self._spec.allowed_tool_ids:
            definition = runtime.defs.get(ref)
            if definition is not None and definition.tool_id in runtime.executors:
                found.append(definition)
        return tuple(found)

    def _admit_tool(self, tool_ref: str) -> ToolDef:
        """許可・定義・供給を検査する。外部は呼ばない。"""
        runtime = self._runtime
        if tool_ref not in self._spec.allowed_tool_ids or runtime is None:
            self._refuse_tool(tool_ref, None, REFUSED_NOT_ALLOWED)
            raise ToolNotAllowedError(
                f"{self._spec.role.value}@{self._spec.spec_version} may not use {tool_ref!r}"
            )
        try:
            tool_id, version = parse_tool_ref(tool_ref)
        except ValueError:
            self._refuse_tool(tool_ref, None, REFUSED_UNKNOWN)
            raise ToolNotAllowedError(f"unknown tool: {tool_ref!r}") from None
        definition = runtime.defs.get(f"{tool_id.value}@{version}")
        if definition is None:
            self._refuse_tool(tool_ref, None, REFUSED_UNKNOWN)
            raise ToolNotAllowedError(f"unknown tool: {tool_ref!r}")
        if tool_id not in runtime.executors:
            self._refuse_tool(tool_ref, definition, REFUSED_NOT_PROVIDED)
            raise ToolNotAllowedError(f"tool {tool_ref} is not enabled for this run")
        return definition

    async def _execute(
        self, definition: ToolDef, ctx: ToolContext, parsed: BaseModel
    ) -> ToolOutcome:
        runtime = self._runtime
        assert runtime is not None
        executor = runtime.executors[definition.tool_id]
        try:
            return await asyncio.wait_for(executor(ctx, parsed), definition.timeout_seconds)
        except TimeoutError:
            return ToolOutcome(status=ToolStatus.TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 想定外の障害は状態 failed にする。本文は残さない
            return ToolOutcome(status=ToolStatus.FAILED, error_code=type(exc).__name__)

    def _envelope(self, call_id: str, definition: ToolDef, outcome: ToolOutcome) -> ToolEnvelope:
        """executor の結果を契約で検証して envelope にする。違反は ``failed``（値を作らない）。"""

        def failed(code: str) -> ToolEnvelope:
            return ToolEnvelope(
                tool_call_id=call_id,
                tool_id=definition.tool_id,
                tool_version=definition.version,
                status=ToolStatus.FAILED,
                limitations=(code,),
            )

        data: dict[str, Any] | None = None
        if outcome.status is ToolStatus.OK:
            if outcome.data is None:
                return failed(ERROR_EXECUTOR_CONTRACT)
            try:
                validated = definition.output_model.model_validate(outcome.data.model_dump())
            except ValidationError:
                return failed(ERROR_OUTPUT_INVALID)
            data = validated.model_dump(mode="json")
        elif outcome.data is not None:
            return failed(ERROR_EXECUTOR_CONTRACT)
        limitations = outcome.limitations
        if outcome.status is ToolStatus.FAILED and outcome.error_code:
            limitations = (outcome.error_code, *limitations)
        try:
            return ToolEnvelope(
                tool_call_id=call_id,
                tool_id=definition.tool_id,
                tool_version=definition.version,
                status=outcome.status,
                data=data,
                limitations=limitations,
                evidence_refs=outcome.evidence_refs,
                retrieved_at=outcome.retrieved_at,
            )
        except ValidationError:
            return failed(ERROR_ENVELOPE_INVALID)

    def _finish_tool(
        self,
        definition: ToolDef,
        envelope: ToolEnvelope,
        seq: int,
        t0: float,
        *,
        cache_hit: bool,
        error_code: str | None,
    ) -> None:
        # 記録は ID・状態・順序だけ。入力・結果の本文は残さない
        self._records.append(
            {
                "tool": definition.ref,
                "tool_ref": definition.ref,
                "tool_call_id": envelope.tool_call_id,
                "seq": seq,
                "status": envelope.status.value,
                "ok": envelope.status is ToolStatus.OK,
                "cache_hit": cache_hit,
            }
        )
        self._notify(
            "completed",
            ToolEvent(
                tool_id=definition.tool_id.value,
                tool_version=definition.version,
                agent_role=self._spec.role.value,
                episode_id=self._runtime.episode_id if self._runtime else "",
                status=envelope.status.value,
                tool_call_id=envelope.tool_call_id,
                duration_ms=_ms(t0),
                cost_class=definition.cost_class,
                cache_hit=cache_hit,
                error_code=error_code,
            ),
        )

    def _refuse_tool(self, tool_ref: str, definition: ToolDef | None, reason: str) -> None:
        self._records.append(
            {
                "tool": tool_ref,
                "tool_ref": tool_ref,
                "ok": False,
                "refused": True,
                "error": "ToolNotAllowedError",
                "reason": reason,
            }
        )
        runtime = self._runtime
        self._notify(
            "refused",
            ToolEvent(
                tool_id=definition.tool_id.value if definition else tool_ref.partition("@")[0],
                tool_version=definition.version if definition else None,
                agent_role=self._spec.role.value,
                episode_id=runtime.episode_id if runtime else "",
                reason=reason,
                cost_class=definition.cost_class if definition else None,
            ),
        )

    def _notify(self, kind: str, event: ToolEvent) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        # 観測の失敗は業務に伝播させない（INV-38）
        with contextlib.suppress(Exception):
            getattr(runtime.observer, kind)(event)

    @staticmethod
    def render_untrusted(envelope: ToolEnvelope) -> str:
        """prompt 用。外部由来の結果を ``<data>`` で区切る（指示として扱わせない。INV-60）。"""
        return render_untrusted(envelope)

    def _require_allowed(self, tool: AgentTool) -> None:
        if tool not in self._spec.allowed_tools:
            self._refuse(tool, "ToolNotAllowedError")
            raise ToolNotAllowedError(
                f"{self._spec.role.value}@{self._spec.spec_version} may not use {tool.value}"
            )
        if tool is not AgentTool.LLM_GENERATE_JSON and tool not in self._tools:
            self._refuse(tool, "ToolNotAllowedError")
            raise ToolNotAllowedError(f"tool {tool.value} is not provided to this run")

    def _refuse(self, tool: AgentTool, error: str) -> None:
        self._records.append({"tool": tool.value, "ok": False, "refused": True, "error": error})

    # --- 記録 ---------------------------------------------------------------

    def invocations(self) -> list[dict[str, Any]]:
        """``agent_runs.tool_invocations`` に入れる JSON-safe な記録（prompt 本文を含まない）。"""
        return [dict(r) for r in self._records]

    def token_usage(self) -> dict[str, int] | None:
        """合計 token。どれか 1 回でも usage が無ければ ``None``（測れた分だけを出さない）。"""
        if any(t is None for t in self._llm_input) or any(t is None for t in self._llm_output):
            return None
        return {
            "input_tokens": sum(t for t in self._llm_input if t is not None),
            "output_tokens": sum(t for t in self._llm_output if t is not None),
            "llm_calls": self._llm_calls,
        }

    def actual_cost_usd(self) -> Decimal | None:
        return sum_costs(self._llm_costs)


def render_untrusted(envelope: ToolEnvelope) -> str:
    """``<data>`` 区切り・区切り文字エスケープ済みの prompt 用ブロック（handler と同じ実装）。"""
    return data_block(f"tool:{envelope.tool_id.value}", envelope.model_dump(mode="json"))


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
