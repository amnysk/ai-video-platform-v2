"""Agent stage の結果の永続化（ADR-0043 §6 Agent stage）。

``persist_stage`` は**1 transaction**（commit は呼び出し側）で次を書く:

1. 成果物（ArtifactStore に正準 JSON を先に置き、読み戻して sha256 を照合してから
   ``artifact_metadata`` の新しい現行 revision にする。前の現行は降りる）
2. ``agent_runs``（``AgentRunRecorder``。費用・token は gateway の実測で、不明は NULL のまま）
3. ``decision_records``（actor ``agent`` / role、``agent_run_id``、``result_revision_id``）と
   ``decision.recorded`` イベント
4. ``revision_links(derived_from)``（読んだ入力 revision → 出力 revision）
5. 品質所見の ``annotations(kind=review_finding)``（scene / time_range に anchor）

冪等キー ``agentic:{episode_id}:{stage_id}:{plan_hash}`` から ``agent_run_id`` が決まる。
同じキーで 2 回目に呼ぶと**何も書かず**保存済みの結果を返す（``load_stage_record``）。

``AgentOutputRejected`` の実行は ``outcome=rejected_output`` の ``agent_runs`` だけを残す
（成果物・判断・注記は作らない）。再試行しても同じ結果を返す（課金済みの呼び出しを繰り返さない）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from contracts.agentic_pipeline import AgentStageOutcome
from contracts.agents import AgentSpec
from contracts.artifact_registry import parse_artifact
from contracts.intervention import (
    ActorType,
    AgentRunOutcome,
    Anchor,
    AnnotationKind,
    InterventionEvent,
    RevisionLinkKind,
    TimeRange,
)
from contracts.states import ArtifactType
from domain.agents.runtime import AgentOutput, AgentOutputRejected
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.errors import ArtifactConflictError
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.intervention.capture import (
    AgentRunRecorder,
    PromptSource,
    RunCapture,
    agent_run_id_for,
    reservation_invocation,
    tool_refs,
)
from infrastructure.intervention.store import Actor, InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore


@dataclass(frozen=True, slots=True)
class StageRecord:
    """保存済みの（または今保存した）stage の結果。"""

    outcome: str
    reason: str | None = None
    artifact_revision_id: str | None = None
    agent_run_id: str | None = None
    decision_id: str | None = None
    #: 提案（JSON）。型を持たず受け渡すだけ。変換は ``ProposeStoryboardChange`` だけが行う（INV-50）
    proposals: tuple[dict[str, Any], ...] = ()
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """gateway の内側で実際に返ってきた LLM 応答（再送せずに再現するための証拠）。"""

    prompt: str
    schema: dict[str, Any]
    data: dict[str, Any]
    model: str
    input_tokens: int | None
    output_tokens: int | None

    def raw(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True, slots=True)
class StageCapture:
    """1 回の stage 実行で記録するもの（handler の外側が集めた事実）。"""

    episode_id: str
    stage_id: str
    idempotency_key: str
    spec: AgentSpec
    #: ``None`` = 出力が契約に合わなかった（``rejected_reason`` が理由）
    output: AgentOutput | None
    #: tool ``intervention:propose`` が積んだ提案（JSON）。出力の提案と一緒に保存し、保存の後に出す
    queued_proposals: Sequence[Mapping[str, Any]] = ()
    rejected_reason: str | None = None
    invocations: Sequence[Mapping[str, Any]] = ()
    token_usage: Mapping[str, Any] | None = None
    cost_usd: Decimal | None = None
    calls: Sequence[LlmCallRecord] = ()
    input_revisions: Mapping[str, str] = field(default_factory=dict)
    extra_refs: Mapping[str, str] = field(default_factory=dict)
    reservation_id: str | None = None
    raw_output_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def agent_spec_ref(spec: AgentSpec) -> dict[str, str]:
    """snapshot に残す版付き Agent の識別（INV-51）。"""
    return {
        "role": spec.role.value,
        "spec_version": spec.spec_version,
        "manifest_hash": spec.manifest_hash,
        "model_policy_ref": spec.model_policy_ref,
    }


def agent_actor(spec: AgentSpec) -> Actor:
    return Actor(ActorType.AGENT, spec.role.value)


# --------------------------------------------------------------------------- 読み戻し


async def load_stage_record(
    session: AsyncSession, store: ArtifactStore, idempotency_key: str
) -> StageRecord | None:
    """同じ冪等キーで保存済みの結果。無ければ ``None``。何も書かない。"""
    istore = InterventionStore(session)
    run_id = agent_run_id_for(idempotency_key)
    run = await istore.get_agent_run(run_id)
    if run is None:
        return None
    stage: dict[str, Any] = {}
    if run.response_object_key is not None:
        stage = dict((await store.get_json(run.response_object_key)).get("stage") or {})
    decision_id = None
    if run.episode_id is not None:
        decision_id = next(
            (
                d.decision_id
                for d in await istore.list_decisions(episode_id=run.episode_id)
                if d.agent_run_id == run_id
            ),
            None,
        )
    outcome = (
        AgentStageOutcome.REJECTED_OUTPUT.value
        if run.outcome is AgentRunOutcome.REJECTED_OUTPUT
        else str(stage.get("outcome") or AgentStageOutcome.PRODUCED.value)
    )
    return StageRecord(
        outcome=outcome,
        reason=stage.get("reason"),
        artifact_revision_id=run.output_revision_id,
        agent_run_id=run_id,
        decision_id=decision_id,
        proposals=tuple(dict(p) for p in stage.get("proposals") or ()),
        replayed=True,
    )


# --------------------------------------------------------------------------- 書き込み


def _proposal_documents(capture: StageCapture) -> list[dict[str, Any]]:
    if capture.output is None:
        return []
    return [p.model_dump(mode="json") for p in capture.output.proposals] + [
        dict(p) for p in capture.queued_proposals
    ]


def _stage_document(capture: StageCapture, outcome: str) -> dict[str, Any]:
    output = capture.output
    return {
        "outcome": outcome,
        "reason": capture.rejected_reason if output is None else output.note,
        "proposals": _proposal_documents(capture),
    }


def check_output_contract(
    spec: AgentSpec, output: AgentOutput
) -> tuple[ArtifactType, str, dict[str, Any]]:
    """成果物が spec の ``output_schema_ref`` どおりの登録済みの版か。違えば Rejected。"""
    assert output.artifact is not None
    document = output.artifact.model_dump(mode="json")
    try:
        parsed = parse_artifact(document["type"], document)  # 登録された版だけを保存する
    except (ValueError, KeyError) as exc:
        raise AgentOutputRejected(f"artifact is not a registered schema: {exc}") from exc
    artifact_type = ArtifactType(document["type"])
    schema_version = str(document["schema_version"])
    if f"{artifact_type.value}@{schema_version}" != spec.output_schema_ref:
        raise AgentOutputRejected(
            f"{spec.role.value} must produce {spec.output_schema_ref}, "
            f"got {artifact_type.value}@{schema_version}"
        )
    return artifact_type, schema_version, parsed.model_dump(mode="json")


async def persist_stage(
    session: AsyncSession,
    store: ArtifactStore,
    *,
    bucket: str,
    capture: StageCapture,
) -> StageRecord:
    """結果を 1 transaction ぶん書く（commit は呼び出し側）。冪等。"""
    existing = await load_stage_record(session, store, capture.idempotency_key)
    if existing is not None:
        return existing

    spec = capture.spec
    output = capture.output
    outcome = (
        AgentStageOutcome.REJECTED_OUTPUT.value
        if output is None
        else {
            "produced": AgentStageOutcome.PRODUCED.value,
            "awaiting_public_metrics": AgentStageOutcome.AWAITING_PUBLIC_METRICS.value,
        }[output.outcome]
    )

    # 1. 成果物（ストアが先。行はその後）
    put_key: str | None = None
    digest: str | None = None
    size: int | None = None
    artifact_type: ArtifactType | None = None
    schema_version: str | None = None
    if output is not None and output.artifact is not None:
        artifact_type, schema_version, canonical = check_output_contract(spec, output)
        digest = sha256_hex(canonical_json_bytes(canonical))
        key = artifact_object_key(capture.episode_id, artifact_type.value, digest)
        put = await store.put_json(key, canonical)
        readback = sha256_hex(canonical_json_bytes(await store.get_json(put.key)))
        if readback != digest or put.sha256 != digest:
            raise ArtifactConflictError(
                f"agent artifact readback sha256 mismatch at {put.key}: "
                f"expected={digest} put={put.sha256} readback={readback}"
            )
        put_key, size = put.key, put.size

    # 2. 実行記録（snapshot を先に保存し、行は成果物と同じ transaction）
    invocations = list(capture.invocations)
    if capture.reservation_id is not None:
        invocations.append(
            reservation_invocation(
                capture.reservation_id,
                provider="agent_llm",
                round=1,
                idempotency_key=capture.idempotency_key,
            )
        )
    prompts = [c.prompt for c in capture.calls]
    run_capture = RunCapture(
        idempotency_key=capture.idempotency_key,
        role=spec.role,
        step=capture.stage_id,
        model=capture.calls[-1].model if capture.calls else None,
        prompt_version=spec.prompt_version,
        prompt="\n\n---\n\n".join(prompts) if prompts else None,
        prompt_source=PromptSource.CAPTURED if prompts else PromptSource.NOT_APPLICABLE,
        input_refs={
            "input_revisions": dict(capture.input_revisions),
            **{k: v for k, v in capture.extra_refs.items()},
        },
        request={
            "calls": [
                {"prompt": c.prompt, "schema": c.schema, "max_tokens": spec.max_tokens}
                for c in capture.calls
            ]
        },
        response={
            "stage": _stage_document(capture, outcome),
            "llm_outputs": [c.raw() for c in capture.calls],
            "raw_output_key": capture.raw_output_key,
        },
        tool_invocations=tuple(invocations),
        episode_id=capture.episode_id,
        outcome=AgentRunOutcome.REJECTED_OUTPUT if output is None else AgentRunOutcome.SUCCEEDED,
        error_summary=capture.rejected_reason if output is None else None,
        token_usage=capture.token_usage,
        started_at=capture.started_at,
        finished_at=capture.finished_at,
        estimated_cost_usd=capture.cost_usd,
        agent_spec=agent_spec_ref(spec),
    )
    recorder = AgentRunRecorder(store)
    stored = await recorder.write(run_capture)

    istore = InterventionStore(session)
    revision_id: str | None = None
    if artifact_type is not None and put_key is not None and digest is not None:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=capture.episode_id,
            artifact_type=artifact_type,
            schema_version=schema_version or "1.0",
            bucket=bucket,
            object_key=put_key,
            sha256=digest,
            size_bytes=size,
            input_hash=sha256_hex(capture.idempotency_key.encode("utf-8")),
        )
        revision_id = meta.id
    run = await recorder.insert(session, run_capture, stored, output_revision_id=revision_id)

    decision_id: str | None = None
    actor = agent_actor(spec)
    if output is not None and output.decision is not None:
        payload = output.decision.model_copy(
            update={
                "result_revision": revision_id,
                "input_snapshot_ref": stored.snapshot_key,
                "tool_invocation_refs": tool_refs(invocations),
                "target_revisions": tuple(dict.fromkeys(capture.input_revisions.values())),
            }
        )
        decision = await istore.insert_decision(
            idempotency_key=f"decision:{payload.decision_type}:{run.agent_run_id}",
            decision_type=payload.decision_type,
            actor=actor,
            payload=payload.model_dump(mode="json"),
            episode_id=capture.episode_id,
            agent_run_id=run.agent_run_id,
            result_revision_id=revision_id,
        )
        decision_id = decision.decision_id
        await istore.append_event(
            capture.episode_id,
            InterventionEvent.DECISION_RECORDED,
            actor,
            {
                "decision_id": decision.decision_id,
                "decision_type": payload.decision_type,
                "agent_run_id": run.agent_run_id,
                "result_revision_id": revision_id,
                **decision.temporal.event_refs(),
            },
        )

    if revision_id is not None:
        for upstream in dict.fromkeys(capture.input_revisions.values()):
            if upstream != revision_id:
                await istore.add_link(
                    capture.episode_id, upstream, revision_id, RevisionLinkKind.DERIVED_FROM
                )

    if output is not None:
        manifest_id = capture.extra_refs.get("manifest")
        if manifest_id is not None and await istore.get_manifest(manifest_id) is None:
            manifest_id = None
        for finding in output.findings:
            anchor = Anchor(
                scene_id=finding.scene_id,
                time_range=None
                if finding.time_range is None
                else TimeRange.model_validate(finding.time_range.model_dump()),
                manifest_id=manifest_id,
            )
            await istore.insert_annotation(
                episode_id=capture.episode_id,
                kind=AnnotationKind.REVIEW_FINDING,
                anchor=anchor.model_dump(mode="json", exclude_none=True),
                body=f"[{finding.severity}] {finding.category}: {finding.evidence}"[:4000],
                actor=actor,
                payload={
                    "finding_id": finding.id,
                    "severity": finding.severity,
                    "category": finding.category,
                    "confidence": finding.confidence,
                    "assessment_kind": "self_assessed",
                },
                manifest_id=manifest_id,
                agent_run_id=run.agent_run_id,
            )

    return StageRecord(
        outcome=outcome,
        reason=_stage_document(capture, outcome)["reason"],
        artifact_revision_id=revision_id,
        agent_run_id=run.agent_run_id,
        decision_id=decision_id,
        proposals=tuple(_proposal_documents(capture)),
    )
