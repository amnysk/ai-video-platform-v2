"""ReviewPacket の組み立てと Markdown（JP-MAS-001 §8、``docs/domain/review-packet.md``）。"""

from __future__ import annotations

from decimal import Decimal

from contracts.intervention import (
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    ChangeKind,
    CommandType,
    Scope,
)
from contracts.review_packet import ReviewPacket
from contracts.states import ArtifactType
from infrastructure.intervention.records import (
    generation_decision_payload,
    record_generation_decision,
)
from infrastructure.intervention.review_packet import build_packet, render_markdown
from infrastructure.intervention.store import Actor, AgentRun, InterventionStore
from tests.unit.test_intervention_read_model import (
    HUMAN,
    _load,
    _put,
    _script,
    _setup,
    _storyboard,
)

SECRETS = (
    "abcdefghijklmnop12345",  # Bearer token
    "SECRETTOKEN123",  # signed URL の query
    "sk-abcdefghijklmnop1234",  # API key
    "deadbeefcafe0042",  # X-Amz-Signature
)


async def test_no_secret_or_signed_url_reaches_the_packet_or_markdown(
    session_factory, artifact_store
) -> None:
    episode_id, s1, _sb = await _setup(session_factory, artifact_store)
    # 素材本体（メタデータ）に token らしき値を含む保存物
    await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.STORYBOARD,
        {
            **_storyboard(s1, description="浮世絵"),
            "metadata": {
                "upload_url": "https://bucket.example.com/a.mp4?X-Amz-Signature=deadbeefcafe0042",
                "api_key": "sk-abcdefghijklmnop1234",
            },
        },
    )
    async with session_factory() as session:
        store = InterventionStore(session)
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": s1.id, "segment_id": "s1"},
            body=(
                "use Authorization: Bearer abcdefghijklmnop12345 and "
                "https://v3.fal.media/files/x.png?token=SECRETTOKEN123"
            ),
            actor=HUMAN,
        )
        await store.insert_command(
            command_id="cmd-secret-1",
            episode_id=episode_id,
            type=CommandType.PROPOSE_CHANGE,
            actor=HUMAN,
            granted_scope=Scope.PROPOSE,
            expected_control_revision=None,
            expected_target_revision=None,
            payload={"note": "sk-abcdefghijklmnop1234"},
            payload_sha256="x",
            correlation_id=None,
        )
        await store.insert_change_set(
            episode_id=episode_id,
            kind=ChangeKind.SCRIPT_EDIT_SCENES,
            base_revision_id=s1.id,
            proposal={"note": "sk-abcdefghijklmnop1234"},
            actor=Actor(ActorType.AI_ASSISTANT, "assistant"),
            proposed_by_command_id="cmd-secret-1",
            impact={"preview": "https://cdn.example.com/p.mp4?token=SECRETTOKEN123"},
            estimated_cost_usd=Decimal("0"),
        )
        await session.commit()

    packet = build_packet(await _load(session_factory, artifact_store, episode_id))
    body = packet.model_dump_json() + render_markdown(packet)
    for secret in SECRETS:
        assert secret not in body, secret
    assert "X-Amz-Signature" not in body
    # 素材は参照だけ（bucket / key / sha256）
    for ref in packet.asset_refs:
        assert ref.object_key is not None and "?" not in ref.object_key
    ReviewPacket.model_validate_json(packet.model_dump_json())


async def test_unknowns_are_listed_instead_of_filled_in(session_factory, artifact_store) -> None:
    episode_id, _s1, _sb = await _setup(session_factory, artifact_store)
    packet = build_packet(await _load(session_factory, artifact_store, episode_id))
    missing = {m.item for m in packet.missing}
    assert {"manifest", "decision:script", "decision:storyboard", "evidence", "cost:llm"} <= missing
    assert packet.costs.llm == "unmeasured"
    assert packet.manifest_persisted is False and packet.manifest_id is None
    assert packet.episode.control.persisted is False


async def test_rationale_is_kept_apart_from_tool_refs_and_results(
    session_factory, artifact_store
) -> None:
    episode_id, s1, _sb = await _setup(session_factory, artifact_store)
    async with session_factory() as session:
        store = InterventionStore(session)
        await store.lock_control(episode_id)
        run: AgentRun = await store.insert_agent_run(
            idempotency_key="run-1",
            role=AgentRole.WRITER_DIRECTOR,
            step="write_script",
            snapshot_object_key="agent-runs/x/context.json",
            snapshot_sha256="0" * 64,
            outcome=AgentRunOutcome.SUCCEEDED,
            episode_id=episode_id,
            output_revision_id=s1.id,
        )
        await record_generation_decision(
            session,
            episode_id=episode_id,
            agent_run=run,
            payload=generation_decision_payload(
                decision_type="write_script",
                question="Write the script",
                candidate_summary="title",
                result_revision=s1.id,
                input_snapshot_ref="agent-runs/x/context.json",
                tool_invocation_refs=("provider_reservation:r-1",),
            ),
        )
        await session.commit()
    packet = build_packet(await _load(session_factory, artifact_store, episode_id))
    (decision,) = [d for d in packet.decisions if d.decision_type == "write_script"]
    assert decision.rationale.kind == "none" and decision.rationale.text is None
    assert decision.tool_invocation_refs == ["provider_reservation:r-1"]
    assert decision.result_revision == s1.id
    assert "decision:script" not in {m.item for m in packet.missing}
    assert "model" in {m.item for m in packet.missing}  # 設定値の model は未検証と明記


async def test_markdown_shows_the_script_old_comments_and_unapplied_commands(
    session_factory, artifact_store
) -> None:
    episode_id, s1, _sb = await _setup(session_factory, artifact_store)
    async with session_factory() as session:
        store = InterventionStore(session)
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": s1.id, "segment_id": "s1"},
            body="硬い",
            actor=HUMAN,
        )
        await store.insert_command(
            command_id="cmd-received-9",
            episode_id=episode_id,
            type=CommandType.COMMENT,
            actor=HUMAN,
            granted_scope=Scope.COMMENT,
            expected_control_revision=None,
            expected_target_revision=None,
            payload={},
            payload_sha256="y",
            correlation_id=None,
        )
        await session.commit()
    await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1="新しい一文"),
    )
    packet = build_packet(await _load(session_factory, artifact_store, episode_id))
    (comment,) = [r for r in packet.reviews if r.kind == "comment"]
    assert comment.old_version is True
    assert packet.commands[0].done is False
    markdown = render_markdown(packet)
    assert "新しい一文" in markdown
    assert "古い版へのコメント" in markdown
    assert "not applied yet" in markdown
    assert "LLM: unmeasured" in markdown
