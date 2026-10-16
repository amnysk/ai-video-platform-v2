"""AgentRun / ContextSnapshot の捕捉。

JP-MAS-001 §6/§7、ADR-0041、``docs/domain/intervention.md`` §6。

1回の役割実行（producer / writer_director / reviewer）を ``agent_runs`` の1行として残す。
prompt 全文・生出力はログに出せない（INV-39）ので、正本は ArtifactStore（本番は MinIO）の
``agent-runs/{episode_id または topic_plan_id}/{agent_run_id}/`` に置き、
行は参照と sha256 だけを持つ。

順序（この module の契約）:

1. ``AgentRunRecorder.write`` が snapshot・request・response を ArtifactStore に書く（行より先）
2. ``AgentRunRecorder.insert`` が呼び出し側の session に行を足す（commit は呼び出し側。成果物の
   ``artifact_metadata`` と**同じ transaction** に入れれば「成果物はあるが記録が無い」が起きない）

- ``agent_run_id`` は ``idempotency_key`` から決定的に導く（``uuid5``）。Activity の再実行は
  同じ id・同じ行になり、記録を増やさない。保存物の名前は内容の sha256 を含むので、
  再実行で内容が変わっても不変性（INV-11）に当たらない。
- model 識別子は**設定値**（``model_source = "configured"``）。Codex CLI が実際に使った
  モデルは検証していない。token usage は解析できたときだけ入れ、取れなければ NULL（0 で埋めない）。
- 保存前に秘密を除去する（ログの安全化と同じ ``sanitize_text``。URL の query・userinfo も落ちる）。

prompt が application code から見えない生成器（storyboard の OpenMontage adapter）は、
``ExchangeRecordingGenerator`` で ``StoryGenerator`` を包み、``recording_exchanges()`` の内側の
呼び出しだけを記録する。tape の外では何もしない（旧経路の挙動を変えない）。
"""

from __future__ import annotations

import contextvars
import hashlib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from contracts.intervention import (
    ActorType,
    AgentRole,
    AgentRunOutcome,
    InterventionEvent,
)
from domain.artifact.hashing import canonical_json_bytes
from domain.errors import NeedsInputError
from domain.script.ports import GenerationRequest, GenerationResult, StoryGenerator
from infrastructure.intervention.store import Actor, AgentRun, InterventionStore
from infrastructure.logging.redaction import sanitize_text
from infrastructure.storage.artifact_store import ArtifactStore

#: 保存先の接頭辞（``agent-runs/{scope}/{agent_run_id}/``）
AGENT_RUNS_PREFIX = "agent-runs"
#: Episode も TopicPlan もまだ無い実行（Topic Planner の候補生成）の一時置き場
STAGING_PREFIX = f"{AGENT_RUNS_PREFIX}/_staging"
CONTEXT_SNAPSHOT_SCHEMA_VERSION = "1.0"
#: model 識別子は設定値。CLI が実際に使ったモデルは未検証（ADR-0041 Consequences）
MODEL_SOURCE_CONFIGURED = "configured"

_AGENT_RUN_NAMESPACE = uuid.UUID("6f1d8a52-3c1e-5b0e-9a57-41a0c2e6b9d1")


class PromptSource(StrEnum):
    """snapshot の ``prompt`` がどこから来たか。"""

    #: 生成器に渡した文字列そのもの
    CAPTURED = "captured"
    #: 再開（保存済みの生出力から続行）で、同じ入力・同じ版のテンプレートから組み直した
    RE_RENDERED = "re_rendered"
    #: 取れなかった（再開で、prompt を組む処理が生成器の内側にある等）。補完しない
    UNAVAILABLE = "unavailable"
    #: LLM を呼ばない役割（レビュアー）
    NOT_APPLICABLE = "not_applicable"


class AgentRunCaptureError(NeedsInputError):
    """新方式の Episode で実行記録を保存できなかった。

    記録の無い成果物を下流（有料生成）へ流さないため Activity を失敗させる。分類は
    ``needs_input``（自動の再ラウンドで LLM 呼び出しを増やさない。予約・生出力は残っているので、
    再開は同じ冪等キーで保存済みの生出力から続く）。
    """


def agent_run_id_for(idempotency_key: str) -> str:
    return str(uuid.uuid5(_AGENT_RUN_NAMESPACE, idempotency_key))


def reservation_invocation(
    reservation_id: str, *, provider: str, round: int, idempotency_key: str
) -> dict[str, Any]:
    """``tool_invocations`` の1件（予約台帳の行を参照する。課金の正本は台帳のまま）。"""
    return {
        "kind": "provider_reservation",
        "id": reservation_id,
        "provider": provider,
        "round": round,
        "idempotency_key": idempotency_key,
    }


def tool_refs(tool_invocations: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """DecisionRecord の ``tool_invocation_refs`` 用（``{kind}:{id}``）。"""
    return tuple(f"{t['kind']}:{t['id']}" for t in tool_invocations if "id" in t)


def request_document(request: GenerationRequest) -> dict[str, Any]:
    return {
        "episode_id": request.episode_id,
        "prompt": request.prompt,
        "output_schema": request.output_schema,
        "timeout_seconds": request.timeout_seconds,
    }


def response_document(
    result: GenerationResult | None, *, raw_text: str, raw_output_key: str | None
) -> dict[str, Any]:
    """生出力（raw output）。再開時は保存済みの生出力しか無いので provider の付帯情報は null。"""
    return {
        "text": raw_text,
        "raw_output_key": raw_output_key,
        "provider_id": None if result is None else result.provider_id,
        "reported_model": None if result is None else result.model,
        "raw_log": None if result is None else (result.raw_log or None),
    }


@dataclass(frozen=True, slots=True)
class RunCapture:
    """1回の役割実行で記録するもの。"""

    idempotency_key: str
    role: AgentRole
    step: str
    model: str | None
    prompt_version: str | None
    prompt: str | None
    prompt_source: PromptSource
    #: 入力の参照（読んだ成果物の revision id・sha256、topic_plan_id 等）
    input_refs: Mapping[str, Any]
    request: Mapping[str, Any] | None
    response: Mapping[str, Any] | None
    tool_invocations: Sequence[Mapping[str, Any]] = ()
    episode_id: str | None = None
    topic_plan_id: str | None = None
    outcome: AgentRunOutcome = AgentRunOutcome.SUCCEEDED
    error_summary: str | None = None
    #: 実際に解析できた値だけ。取れなければ None（0 で埋めない）
    token_usage: Mapping[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: 追加の補足（例: 再開で prompt を組み直した理由）
    notes: tuple[str, ...] = ()
    #: 実績費用（USD）。測れたときだけ。None は不明で、``agent_runs.estimated_cost_usd`` は NULL
    #: のまま（0 で埋めない / ADR-0043 §2）
    estimated_cost_usd: Decimal | None = None
    #: 版付き Agent の識別（role, spec_version, manifest_hash, model_policy_ref）。snapshot に残す。
    #: None（従来の経路）なら snapshot に項目を足さない
    agent_spec: Mapping[str, Any] | None = None

    @property
    def agent_run_id(self) -> str:
        return agent_run_id_for(self.idempotency_key)

    @property
    def scope(self) -> str:
        scope = self.episode_id or self.topic_plan_id
        if not scope:
            raise ValueError("an agent run needs an episode_id or a topic_plan_id")
        return scope


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    agent_run_id: str
    snapshot_key: str
    snapshot_sha256: str
    request_key: str | None
    request_sha256: str | None
    response_key: str | None
    response_sha256: str | None
    redaction_applied: bool


# --------------------------------------------------------------------------- 安全化


class _Redaction:
    def __init__(self) -> None:
        self.applied = False

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            out, changed = sanitize_text(value)
            self.applied = self.applied or changed
            return out
        if isinstance(value, Mapping):
            return {str(k): self.value(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value


def snapshot_document(capture: RunCapture) -> tuple[dict[str, Any], bool]:
    """ContextSnapshot の JSON（安全化済み）と、置換が起きたか。時刻は入れない（決定的にする）。"""
    redaction = _Redaction()
    document = {
        "schema_version": CONTEXT_SNAPSHOT_SCHEMA_VERSION,
        "kind": "context_snapshot",
        "agent_run_id": capture.agent_run_id,
        "role": capture.role.value,
        "step": capture.step,
        "scope": {"episode_id": capture.episode_id, "topic_plan_id": capture.topic_plan_id},
        "model": capture.model,
        "model_source": MODEL_SOURCE_CONFIGURED,
        "prompt_version": capture.prompt_version,
        "prompt_source": capture.prompt_source.value,
        "prompt": redaction.value(capture.prompt),
        "input_refs": redaction.value(dict(capture.input_refs)),
        "request": redaction.value(None if capture.request is None else dict(capture.request)),
        "response": redaction.value(None if capture.response is None else dict(capture.response)),
        "tool_invocations": [dict(t) for t in capture.tool_invocations],
        "notes": list(capture.notes),
    }
    if capture.agent_spec is not None:
        document["agent_spec"] = dict(capture.agent_spec)
    document["redaction_applied"] = redaction.applied
    return document, redaction.applied


def _named(prefix: str, name: str, body: bytes) -> str:
    return f"{prefix}{name}-{hashlib.sha256(body).hexdigest()[:16]}.json"


# --------------------------------------------------------------------------- recorder


class AgentRunRecorder:
    """snapshot を ArtifactStore に書いてから ``agent_runs`` の行を足す。commit は呼び出し側。"""

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._store = artifact_store

    async def write(self, capture: RunCapture) -> StoredSnapshot:
        """snapshot・request・response を保存する。失敗は ``AgentRunCaptureError``。"""
        prefix = f"{AGENT_RUNS_PREFIX}/{capture.scope}/{capture.agent_run_id}/"
        document, applied = snapshot_document(capture)
        try:
            snapshot = await self._put(prefix, "context", document)
            request = (
                None
                if document["request"] is None
                else await self._put(prefix, "request", document["request"])
            )
            response = (
                None
                if document["response"] is None
                else await self._put(prefix, "response", document["response"])
            )
        except Exception as exc:
            raise AgentRunCaptureError(
                f"agent run snapshot could not be stored ({type(exc).__name__})"
            ) from exc
        return StoredSnapshot(
            agent_run_id=capture.agent_run_id,
            snapshot_key=snapshot[0],
            snapshot_sha256=snapshot[1],
            request_key=None if request is None else request[0],
            request_sha256=None if request is None else request[1],
            response_key=None if response is None else response[0],
            response_sha256=None if response is None else response[1],
            redaction_applied=applied,
        )

    async def _put(self, prefix: str, name: str, payload: Mapping[str, Any]) -> tuple[str, str]:
        key = _named(prefix, name, canonical_json_bytes(payload))
        put = await self._store.put_json(key, payload)
        return put.key, put.sha256

    async def stage(self, key: str, capture: RunCapture) -> None:
        """所属（episode / topic_plan）がまだ無い実行の記録を一時置き場に書く（安全化済み）。

        Topic Planner の候補生成は TopicPlan より前に走る。plan を保存する transaction で
        ``load_stage`` → ``record`` し、正規の ``agent-runs/{topic_plan_id}/…`` へ写す。
        """
        if not key.startswith(f"{STAGING_PREFIX}/"):
            raise ValueError(f"staging key must be under {STAGING_PREFIX}/: {key}")
        redaction = _Redaction()
        document = {
            "schema_version": CONTEXT_SNAPSHOT_SCHEMA_VERSION,
            "kind": "staged_agent_run",
            "idempotency_key": capture.idempotency_key,
            "role": capture.role.value,
            "step": capture.step,
            "model": capture.model,
            "prompt_version": capture.prompt_version,
            "prompt": redaction.value(capture.prompt),
            "prompt_source": capture.prompt_source.value,
            "input_refs": redaction.value(dict(capture.input_refs)),
            "request": redaction.value(None if capture.request is None else dict(capture.request)),
            "response": redaction.value(
                None if capture.response is None else dict(capture.response)
            ),
            "tool_invocations": [dict(t) for t in capture.tool_invocations],
            "notes": list(capture.notes),
        }
        try:
            await self._store.put_json(key, document)
        except Exception as exc:
            raise AgentRunCaptureError(
                f"agent run staging could not be stored ({type(exc).__name__})"
            ) from exc

    async def load_stage(self, key: str) -> RunCapture | None:
        """``stage`` で書いた記録。無ければ ``None``（所属は呼び出し側が ``replace`` で足す）。"""
        try:
            document = await self._store.get_json(key)
        except KeyError:
            return None
        return RunCapture(
            idempotency_key=str(document["idempotency_key"]),
            role=AgentRole(document["role"]),
            step=str(document["step"]),
            model=document.get("model"),
            prompt_version=document.get("prompt_version"),
            prompt=document.get("prompt"),
            prompt_source=PromptSource(document["prompt_source"]),
            input_refs=dict(document.get("input_refs") or {}),
            request=document.get("request"),
            response=document.get("response"),
            tool_invocations=tuple(document.get("tool_invocations") or ()),
            notes=tuple(document.get("notes") or ()),
        )

    async def insert(
        self,
        session: AsyncSession,
        capture: RunCapture,
        stored: StoredSnapshot,
        *,
        output_revision_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentRun:
        """行を足す（同じ冪等キーなら既存行）。Episode があれば ``agent.run.recorded`` も足す。"""
        store = InterventionStore(session)
        existing = await store.get_agent_run(capture.agent_run_id)
        if existing is not None:
            return existing  # Activity の再実行。行もイベントも増やさない
        run = await store.insert_agent_run(
            agent_run_id=capture.agent_run_id,
            idempotency_key=capture.idempotency_key,
            role=capture.role,
            step=capture.step,
            snapshot_object_key=stored.snapshot_key,
            snapshot_sha256=stored.snapshot_sha256,
            outcome=capture.outcome,
            episode_id=capture.episode_id,
            topic_plan_id=capture.topic_plan_id,
            model=capture.model,
            prompt_version=capture.prompt_version,
            request_object_key=stored.request_key,
            request_sha256=stored.request_sha256,
            response_object_key=stored.response_key,
            response_sha256=stored.response_sha256,
            tool_invocations=list(capture.tool_invocations),
            token_usage=None if capture.token_usage is None else dict(capture.token_usage),
            estimated_cost_usd=capture.estimated_cost_usd,
            output_revision_id=output_revision_id,
            error_summary=capture.error_summary,
            correlation_id=correlation_id,
            started_at=capture.started_at,
            finished_at=capture.finished_at,
        )
        if capture.episode_id is not None:
            await store.append_event(
                capture.episode_id,
                InterventionEvent.AGENT_RUN_RECORDED,
                Actor(ActorType.AGENT, capture.role.value),
                {
                    "agent_run_id": run.agent_run_id,
                    "role": capture.role.value,
                    "step": capture.step,
                    "output_revision_id": output_revision_id,
                    **run.temporal.event_refs(),
                },
            )
        return run

    async def record(
        self,
        session: AsyncSession,
        capture: RunCapture,
        *,
        output_revision_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentRun:
        """``write`` → ``insert``。commit は呼び出し側。"""
        stored = await self.write(capture)
        return await self.insert(
            session,
            capture,
            stored,
            output_revision_id=output_revision_id,
            correlation_id=correlation_id,
        )


# --------------------------------------------------------------------------- LLM 呼び出しの tape


@dataclass(frozen=True, slots=True)
class LlmExchange:
    request: GenerationRequest
    result: GenerationResult | None
    #: 失敗した呼び出しの例外の型名（本文は秘密を含みうるので残さない）
    error: str | None = None


@dataclass(slots=True)
class ExchangeTape:
    exchanges: list[LlmExchange] = field(default_factory=list)

    def last_for(self, episode_id: str) -> LlmExchange | None:
        for exchange in reversed(self.exchanges):
            if exchange.request.episode_id == episode_id:
                return exchange
        return None


_TAPE: contextvars.ContextVar[ExchangeTape | None] = contextvars.ContextVar(
    "agent_run_exchange_tape", default=None
)


@contextmanager
def recording_exchanges() -> Iterator[ExchangeTape]:
    """この文脈（asyncio task）の中の ``ExchangeRecordingGenerator`` 呼び出しを記録する。"""
    tape = ExchangeTape()
    token = _TAPE.set(tape)
    try:
        yield tape
    finally:
        _TAPE.reset(token)


class ExchangeRecordingGenerator:
    """``StoryGenerator`` を包み、tape があるときだけ request／result を写す。中身は変えない。"""

    def __init__(self, inner: StoryGenerator) -> None:
        self._inner = inner

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        tape = _TAPE.get()
        if tape is None:
            return await self._inner.generate(request)
        try:
            result = await self._inner.generate(request)
        except BaseException as exc:
            tape.exchanges.append(LlmExchange(request, None, type(exc).__name__))
            raise
        tape.exchanges.append(LlmExchange(request, result))
        return result


__all__ = [
    "AGENT_RUNS_PREFIX",
    "CONTEXT_SNAPSHOT_SCHEMA_VERSION",
    "MODEL_SOURCE_CONFIGURED",
    "STAGING_PREFIX",
    "AgentRunCaptureError",
    "AgentRunRecorder",
    "ExchangeRecordingGenerator",
    "ExchangeTape",
    "LlmExchange",
    "PromptSource",
    "RunCapture",
    "StoredSnapshot",
    "agent_run_id_for",
    "recording_exchanges",
    "request_document",
    "reservation_invocation",
    "response_document",
    "snapshot_document",
    "tool_refs",
]
