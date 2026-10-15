"""判断の説明と Episode 横断の判断検索（ADR-0042 §5）。

理由: docs/testing/assistant-rationale.md §2。

store と SQLite は本物。LLM・ネットワークは無い。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.testing import ActivityEnvironment

from apps.api.auth import parse_principals, token_sha256
from contracts.intervention import (
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    CommandType,
    RevisionLinkKind,
    Scope,
)
from contracts.states import ArtifactType
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention.store import Actor, InterventionStore

PREFIX = "/v1/intervention"
READ_TOKEN = "reader-token-insights-01"
NO_READ_TOKEN = "commenter-token-insights-02"
AGENT = Actor(ActorType.AGENT, "writer_director")
HUMAN = Actor(ActorType.HUMAN, "owner")


def _principals() -> str:
    return json.dumps(
        {
            token_sha256(READ_TOKEN): {
                "actor_type": "ai_assistant",
                "actor_id": "chat",
                "scopes": ["read"],
            },
            token_sha256(NO_READ_TOKEN): {
                "actor_type": "ai_assistant",
                "actor_id": "chat2",
                "scopes": ["comment"],
            },
        }
    )


def _auth(token: str = READ_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Seed:
    client: AsyncClient
    session_factory: Any
    episode_id: str
    decision_id: str
    up: str
    down: str


async def _revision(session, episode_id, artifact_type, key: str) -> str:
    row = await ArtifactMetadataRepository(session).record(
        episode_id=episode_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        bucket="b",
        object_key=key,
        sha256=(key * 64)[:64],
        size_bytes=1,
    )
    return str(row.id)


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision_type": "script_structure",
        "question": "冒頭の構成はどれにするか",
        "candidates": [
            {"id": "a", "summary": "問いかけ始まり", "scores": {"fit": 0.8}},
            {"id": "b", "summary": "結論始まり", "rejected_reason": "弱い"},
        ],
        "selected_candidate": "a",
        "rationale_kind": "reported",
        "rationale": "視聴者の関心に合う",
        "evidence_refs": ["research:1", "research:2"],
        "evidence_check": "unverified_ref",
        "input_snapshot_ref": "agent-runs/x/context.json",
        "constraints": ["尺は60秒以内"],
        "uncertainties": ["数字の出典が弱い"],
    }
    base.update(over)
    return base


@pytest_asyncio.fixture
async def seed(session_factory) -> AsyncIterator[Seed]:
    from apps.api.main import create_app
    from apps.api.routers import intervention, intervention_insights

    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="dummy")
        episode_id = str(episode.id)
        up = await _revision(session, episode.id, ArtifactType.SCRIPT, "1")
        down = await _revision(session, episode.id, ArtifactType.STORYBOARD, "2")
        store = InterventionStore(session)
        run = await store.insert_agent_run(
            idempotency_key="run-1",
            role=AgentRole.WRITER_DIRECTOR,
            step="script",
            snapshot_object_key="agent-runs/x/context.json",
            snapshot_sha256="a" * 64,
            outcome=AgentRunOutcome.SUCCEEDED,
            episode_id=episode_id,
            model="codex-cli",
            prompt_version="script-v3",
            output_revision_id=up,
        )
        decision = await store.insert_decision(
            idempotency_key="d-1",
            decision_type="script_structure",
            actor=AGENT,
            payload=_payload(target_revisions=[up], result_revision=up),
            episode_id=episode_id,
            agent_run_id=run.agent_run_id,
            result_revision_id=up,
        )
        await store.add_link(episode_id, up, down, RevisionLinkKind.DERIVED_FROM)
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": up, "segment_id": "s1"},
            body="ここは短く",
            actor=HUMAN,
            command_id="c-comment-1",
        )
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": down},
            body="別の版へのコメント",
            actor=HUMAN,
            command_id="c-comment-2",
        )
        envelope_payload = {"change": {"kind": "script.edit_scenes", "base_revision_id": up}}
        await store.insert_command(
            command_id="cmd-propose-1",
            episode_id=episode_id,
            type=CommandType.PROPOSE_CHANGE,
            actor=Actor(ActorType.AI_ASSISTANT, "chat"),
            granted_scope=Scope.PROPOSE,
            expected_control_revision=None,
            expected_target_revision=up,
            payload=envelope_payload,
            payload_sha256="f" * 64,
            correlation_id=None,
        )
        await session.commit()

    app = create_app()
    app.dependency_overrides[intervention.get_principal_registry] = lambda: parse_principals(
        _principals()
    )
    app.dependency_overrides[intervention_insights.get_insights_session_factory] = lambda: (
        session_factory
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Seed(client, session_factory, episode_id, decision.decision_id, up, down)


def _url(seed: Seed, decision_id: str | None = None) -> str:
    target = decision_id or seed.decision_id
    return f"{PREFIX}/episodes/{seed.episode_id}/decisions/{target}/explanation"


# --------------------------------------------------------------------------- explanation


async def test_explanation_is_built_from_stored_rows(seed: Seed) -> None:
    response = await seed.client.get(_url(seed), headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision_id"] == seed.decision_id
    assert body["question"] == "冒頭の構成はどれにするか"
    assert body["selected_candidate"] == "a"
    selected = [c["id"] for c in body["candidates"] if c["selected"]]
    assert selected == ["a"]
    rejected = next(c for c in body["candidates"] if c["id"] == "b")
    assert rejected["rejected_reason"] == "弱い"
    run = body["agent_run"]
    assert (run["role"], run["model"], run["prompt_version"]) == (
        "writer_director",
        "codex-cli",
        "script-v3",
    )
    assert body["constraints"] == ["尺は60秒以内"]
    assert body["uncertainties"] == ["数字の出典が弱い"]
    assert body["input_snapshot_ref"] == "agent-runs/x/context.json"
    assert [d["downstream_revision_id"] for d in body["downstream_revisions"]] == [seed.down]


async def test_reported_rationale_is_labelled_self_reported_not_verified(seed: Seed) -> None:
    body = (await seed.client.get(_url(seed), headers=_auth())).json()
    assert body["rationale"]["kind"] == "reported"  # 保存された値をそのまま
    assert body["rationale"]["text"] == "視聴者の関心に合う"
    assert body["rationale"]["provenance"] == "self_reported_by_model"
    assert body["rationale"]["verified"] is False
    assert body["evidence"]["evidence_check"] == "unverified_ref"
    assert body["evidence"]["refs"] == ["research:1", "research:2"]


async def test_missing_data_is_reported_as_not_recorded_never_filled(seed: Seed) -> None:
    body = (await seed.client.get(_url(seed), headers=_auth())).json()
    # token usage / cost は agent_runs が NULL → 0 ではなく null + 欠測の一覧
    assert body["agent_run"]["token_usage"] is None
    assert body["agent_run"]["estimated_cost_usd"] is None
    assert {"agent_run.token_usage", "agent_run.estimated_cost_usd"} <= set(body["not_recorded"])


async def test_a_decision_without_rationale_or_agent_run_lists_what_is_missing(
    seed: Seed,
) -> None:
    async with seed.session_factory() as session:
        bare = await InterventionStore(session).insert_decision(
            idempotency_key="d-bare",
            decision_type="topic_choice",
            actor=HUMAN,
            payload=_payload(
                rationale_kind="none",
                rationale=None,
                evidence_refs=[],
                evidence_check="not_checked",
                input_snapshot_ref=None,
                constraints=[],
                uncertainties=[],
            ),
            episode_id=seed.episode_id,
        )
        await session.commit()
    body = (await seed.client.get(_url(seed, bare.decision_id), headers=_auth())).json()
    assert body["rationale"]["kind"] == "none"
    assert body["rationale"]["text"] is None
    assert body["rationale"]["provenance"] == "not_recorded"
    assert body["agent_run"] is None
    assert {
        "rationale",
        "agent_run",
        "evidence_refs",
        "input_snapshot_ref",
        "result_revision",
    } <= set(body["not_recorded"])
    assert body["downstream_revisions"] == []
    assert body["interventions"] == {"annotations": [], "commands": []}


async def test_recorded_token_usage_and_cost_are_shown_as_recorded(seed: Seed) -> None:
    async with seed.session_factory() as session:
        store = InterventionStore(session)
        run = await store.insert_agent_run(
            idempotency_key="run-usage",
            role=AgentRole.REVIEWER,
            step="review",
            snapshot_object_key="agent-runs/y/context.json",
            snapshot_sha256="b" * 64,
            outcome=AgentRunOutcome.SUCCEEDED,
            episode_id=seed.episode_id,
            token_usage={"input": 100, "output": 20},
            estimated_cost_usd=Decimal("0.0123"),
        )
        d = await store.insert_decision(
            idempotency_key="d-usage",
            decision_type="review",
            actor=AGENT,
            payload=_payload(),
            episode_id=seed.episode_id,
            agent_run_id=run.agent_run_id,
        )
        await session.commit()
    body = (await seed.client.get(_url(seed, d.decision_id), headers=_auth())).json()
    assert body["agent_run"]["token_usage"] == {"input": 100, "output": 20}
    assert body["agent_run"]["estimated_cost_usd"] == 0.0123
    assert "agent_run.token_usage" not in body["not_recorded"]


async def test_interventions_target_only_the_decisions_revisions(seed: Seed) -> None:
    body = (await seed.client.get(_url(seed), headers=_auth())).json()
    assert [a["body"] for a in body["interventions"]["annotations"]] == ["ここは短く"]
    assert [c["command_id"] for c in body["interventions"]["commands"]] == ["cmd-propose-1"]
    assert body["interventions"]["commands"][0]["actor"] == {
        "type": "ai_assistant",
        "id": "chat",
    }


async def test_unknown_or_foreign_decision_is_404(seed: Seed, session_factory) -> None:
    missing = await seed.client.get(_url(seed, str(uuid.uuid4())), headers=_auth())
    assert missing.status_code == 404
    async with session_factory() as session:
        other = await EpisodeRepository(session).create(topic="other")
        foreign = await InterventionStore(session).insert_decision(
            idempotency_key="d-foreign",
            decision_type="x",
            actor=HUMAN,
            payload=_payload(),
            episode_id=other.id,
        )
        await session.commit()
    response = await seed.client.get(_url(seed, foreign.decision_id), headers=_auth())
    assert response.status_code == 404  # 別 Episode の判断を、この Episode の経路で読ませない


async def test_explanation_requires_read_scope(seed: Seed) -> None:
    assert (await seed.client.get(_url(seed))).status_code == 401
    forbidden = await seed.client.get(_url(seed), headers=_auth(NO_READ_TOKEN))
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["error"] == "missing_scope"


# --------------------------------------------------------------------------- search


async def _add_decisions(seed: Seed, n: int, *, decision_type: str, episode_id: str) -> list[str]:
    ids: list[str] = []
    async with seed.session_factory() as session:
        store = InterventionStore(session)
        for i in range(n):
            d = await store.insert_decision(
                idempotency_key=f"bulk-{decision_type}-{episode_id}-{i}",
                decision_type=decision_type,
                actor=AGENT,
                payload=_payload(decision_type=decision_type),
                episode_id=episode_id,
            )
            ids.append(d.decision_id)
        await session.commit()
    return ids


async def test_search_filters_by_type_role_and_episode(seed: Seed, session_factory) -> None:
    async with session_factory() as session:
        other = await EpisodeRepository(session).create(topic="other")
        other_id = str(other.id)
        await session.commit()
    await _add_decisions(seed, 2, decision_type="topic_choice", episode_id=other_id)
    url = f"{PREFIX}/decisions"
    everything = (await seed.client.get(url, headers=_auth())).json()["decisions"]
    assert len(everything) == 3  # Episode を跨ぐ
    by_type = (
        await seed.client.get(url, params={"decision_type": "topic_choice"}, headers=_auth())
    ).json()
    assert {d["decision_type"] for d in by_type["decisions"]} == {"topic_choice"}
    by_ep = (
        await seed.client.get(url, params={"episode_id": seed.episode_id}, headers=_auth())
    ).json()
    assert [d["decision_id"] for d in by_ep["decisions"]] == [seed.decision_id]
    by_role = (
        await seed.client.get(url, params={"role": "writer_director"}, headers=_auth())
    ).json()
    assert [d["decision_id"] for d in by_role["decisions"]] == [seed.decision_id]
    assert by_role["decisions"][0]["role"] == "writer_director"
    bad = await seed.client.get(url, params={"role": "ceo"}, headers=_auth())
    assert bad.status_code == 422


async def test_search_filters_by_scene_and_summaries_show_the_links(
    seed: Seed, session_factory
) -> None:
    async with session_factory() as session:
        store = InterventionStore(session)
        scoped = await store.insert_decision(
            idempotency_key="d-scene",
            decision_type="apply_change",
            actor=AGENT,
            payload=_payload(decision_type="apply_change"),
            episode_id=seed.episode_id,
            scene_id="sb2",
        )
        await session.commit()
    url = f"{PREFIX}/decisions"
    page = (await seed.client.get(url, params={"scene_id": "sb2"}, headers=_auth())).json()
    assert [d["decision_id"] for d in page["decisions"]] == [scoped.decision_id]
    assert page["decisions"][0]["scene_id"] == "sb2"
    none = (await seed.client.get(url, params={"scene_id": "sb9"}, headers=_auth())).json()
    assert none["decisions"] == []


async def test_explanation_shows_scene_and_workflow_links_or_lists_them_as_not_recorded(
    seed: Seed, session_factory
) -> None:
    # seed の判断は Activity の外で作った: 識別子は null で not_recorded に載る（補完しない）
    bare = (await seed.client.get(_url(seed), headers=_auth())).json()
    assert (bare["scene_id"], bare["workflow_id"], bare["workflow_run_id"]) == (None,) * 3
    assert {"scene_id", "workflow_id", "workflow_run_id"} <= set(bare["not_recorded"])
    assert bare["agent_run"]["workflow_id"] is None
    assert "agent_run.workflow_id" in bare["not_recorded"]

    async with session_factory() as session:
        store = InterventionStore(session)
        d = await ActivityEnvironment().run(_insert_linked, store, seed.episode_id)
        await session.commit()
    body = (await seed.client.get(_url(seed, d.decision_id), headers=_auth())).json()
    assert (body["scene_id"], body["workflow_id"], body["workflow_run_id"]) == (
        "sb2",
        "test",
        "test-run",
    )
    assert not {"scene_id", "workflow_id", "workflow_run_id"} & set(body["not_recorded"])
    assert (body["agent_run"]["workflow_id"], body["agent_run"]["activity_id"]) == ("test", "test")
    assert "agent_run.workflow_id" not in body["not_recorded"]


async def _insert_linked(store: InterventionStore, episode_id: str):
    run = await store.insert_agent_run(
        idempotency_key="run-linked",
        role=AgentRole.WRITER_DIRECTOR,
        step="write_script",
        snapshot_object_key="agent-runs/z/context.json",
        snapshot_sha256="c" * 64,
        outcome=AgentRunOutcome.SUCCEEDED,
        episode_id=episode_id,
    )
    return await store.insert_decision(
        idempotency_key="d-linked",
        decision_type="apply_change",
        actor=AGENT,
        payload=_payload(decision_type="apply_change"),
        episode_id=episode_id,
        agent_run_id=run.agent_run_id,
        scene_id="sb2",
    )


async def test_search_paginates_with_a_keyset_cursor_and_caps_the_limit(seed: Seed) -> None:
    await _add_decisions(seed, 6, decision_type="bulk", episode_id=seed.episode_id)
    url = f"{PREFIX}/decisions"
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["before"] = cursor
        page = (await seed.client.get(url, params=params, headers=_auth())).json()
        seen.extend(d["decision_id"] for d in page["decisions"])
        cursor = page["next_before"]
        if cursor is None:
            break
    assert len(seen) == 7 and len(set(seen)) == 7  # 欠落・重複なし
    too_big = await seed.client.get(url, params={"limit": 100000}, headers=_auth())
    assert too_big.status_code == 422
    bad_cursor = await seed.client.get(url, params={"before": str(uuid.uuid4())}, headers=_auth())
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["detail"]["error"] == "invalid_cursor"


async def test_search_requires_read_scope(seed: Seed) -> None:
    assert (await seed.client.get(f"{PREFIX}/decisions")).status_code == 401
    assert (
        await seed.client.get(f"{PREFIX}/decisions", headers=_auth(NO_READ_TOKEN))
    ).status_code == 403


def test_the_new_routes_are_read_only_gets() -> None:
    from apps.api.routers.intervention_insights import router

    methods = {m for r in router.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET"}
