"""tool 呼び出しの一覧・詳細・改善トレース（ADR-0047 §7）。SQLite + InMemoryArtifactStore。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import update

from contracts.agent_tools import ToolId, ToolStatus
from contracts.application import OperationErrorCode
from contracts.application_operations import (
    TRACE_LINKS,
    ImprovementTrace,
    ListToolCallsRequest,
    TraceResult,
)
from contracts.intervention import AgentRole, CommandType
from contracts.states import ArtifactType
from infrastructure.db.agent_tool_call_repository import tool_call_object_key
from infrastructure.db.models import AgentToolCallRow, ArtifactMetadataRow
from tests.support.intervention import HUMAN, envelope, receive
from tests.support.tool_evidence_world import RESULT_BODY, SECRET, World, make_world

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world(session_factory, artifact_store) -> World:
    return await make_world(session_factory, artifact_store)


async def _flag(world: World, target: str, flag: str) -> None:
    env = envelope(
        CommandType.COMMENT,
        {
            "anchor": {"target_ref": target},
            "body": "this is wrong",
            "kind": "review_finding",
            "flag": flag,
        },
    )
    await receive(world.session_factory, world.episode_id, env, HUMAN)


async def _expect(awaitable, code: OperationErrorCode):
    from application.errors import OperationError

    with pytest.raises(OperationError) as err:
        await awaitable
    assert err.value.code is code
    return err.value


# --------------------------------------------------------------------------- list


async def test_list_returns_summaries_newest_first_without_result_bodies(world: World) -> None:
    a = await world.tool_call(1)
    b = await world.tool_call(2, tool=ToolId.GET_VIDEO_ANALYTICS, role="learning")
    page = await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    assert [c.tool_call_id for c in page.calls] == [b, a]
    first = page.calls[0]
    assert first.tool_ref == "get_video_analytics@1" and first.agent_ref == "learning@1.1"
    assert (first.seq, first.status, first.duration_ms) == (2, "ok", 2000)
    assert first.evidence_refs == ("research_request:r2",) and first.research_request_id == "r2"
    assert first.finished_at is not None and first.error_code is None
    assert RESULT_BODY not in page.model_dump_json()  # 本文は詳細だけ


async def test_list_filters_by_role_and_tool(world: World) -> None:
    s = await world.tool_call(1)
    await world.tool_call(2, tool=ToolId.GET_VIDEO_ANALYTICS, role="learning")
    by_role = await world.reader.list_tool_calls(
        world.eid, ListToolCallsRequest(agent_role=AgentRole.STRATEGY)
    )
    assert [c.tool_call_id for c in by_role.calls] == [s]
    by_tool = await world.reader.list_tool_calls(
        world.eid, ListToolCallsRequest(tool_id=ToolId.GET_VIDEO_ANALYTICS)
    )
    assert [c.agent_role for c in by_tool.calls] == ["learning"]


async def test_list_pages_with_a_composite_cursor_and_loses_nothing_on_equal_times(
    world: World,
) -> None:
    from tests.support.tool_evidence_world import T0

    ids = [await world.tool_call(n, at=T0) for n in range(1, 6)]  # 同時刻の 5 件
    seen: list[str] = []
    request = ListToolCallsRequest(limit=2)
    for _ in range(5):
        page = await world.reader.list_tool_calls(world.eid, request)
        seen += [c.tool_call_id for c in page.calls]
        if page.next_before is None:
            break
        assert page.next_before_id is not None
        request = ListToolCallsRequest(
            limit=2, before=page.next_before, before_id=page.next_before_id
        )
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))


async def test_a_cursor_must_be_a_pair() -> None:
    from datetime import UTC, datetime

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ListToolCallsRequest(before=datetime.now(UTC))


async def test_list_shows_only_this_episodes_calls_and_unknown_episode_is_404(
    world: World, session_factory, artifact_store
) -> None:
    other = await make_world(session_factory, artifact_store)
    await other.tool_call(1)
    mine = await world.tool_call(1, tool_input={"region": "US"})
    page = await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    assert [c.tool_call_id for c in page.calls] == [mine]
    await _expect(
        world.reader.list_tool_calls(uuid.uuid4(), ListToolCallsRequest()),
        OperationErrorCode.EPISODE_NOT_FOUND,
    )


async def test_secret_looking_input_keys_are_redacted(world: World) -> None:
    await world.tool_call(1, tool_input={"region": "JP", "api_key": SECRET, "x": {"token": SECRET}})
    page = await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    dumped = page.model_dump_json()
    assert SECRET not in dumped
    assert page.calls[0].input["region"] == "JP" and page.calls[0].input["api_key"] == "[redacted]"


# --------------------------------------------------------------------------- 注記が根拠の横に出る


async def test_a_human_flag_shows_next_to_the_tool_call_in_list_and_detail(world: World) -> None:
    call = await world.tool_call(1)
    other = await world.tool_call(2)
    await _flag(world, f"tool_call:{call}", "wrong_evidence")
    page = await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    by_id = {c.tool_call_id: c for c in page.calls}
    assert by_id[other].annotations == ()
    (note,) = by_id[call].annotations
    assert note["anchor"] == {"target_ref": f"tool_call:{call}"}
    assert note["payload"] == {"flag": "wrong_evidence"} and note["status"] == "open"
    detail = await world.reader.get_tool_call(world.eid, call)
    assert detail.annotations == (note,)


# --------------------------------------------------------------------------- detail


async def test_detail_reads_the_result_back_verified_and_untrusted(world: World) -> None:
    call = await world.tool_call(1)
    detail = await world.reader.get_tool_call(world.eid, call)
    assert detail.integrity == "verified" and detail.result_untrusted is True
    assert detail.result is not None
    assert detail.result["untrusted"] is True and detail.result["status"] == ToolStatus.OK.value
    assert detail.result["data"] == {"answer": RESULT_BODY}
    assert detail.result_sha256 is not None and detail.result_missing_reason is None


async def test_a_sha256_mismatch_is_an_explicit_integrity_error(
    world: World, artifact_store
) -> None:
    call = await world.tool_call(1)
    key = tool_call_object_key(world.episode_id, call)
    artifact_store._objects[key] = b'{"tampered": true}'  # noqa: SLF001 - 改ざんの再現
    err = await _expect(
        world.reader.get_tool_call(world.eid, call), OperationErrorCode.INTEGRITY_ERROR
    )
    assert err.detail["reason"] == "sha256_mismatch" and err.detail["actual_sha256"]
    assert "tampered" not in json.dumps(err.detail)  # 中身は返さない


async def test_a_missing_result_object_is_an_integrity_error_not_an_empty_result(
    world: World, artifact_store
) -> None:
    call = await world.tool_call(1)
    del artifact_store._objects[tool_call_object_key(world.episode_id, call)]  # noqa: SLF001
    err = await _expect(
        world.reader.get_tool_call(world.eid, call), OperationErrorCode.INTEGRITY_ERROR
    )
    assert err.detail["reason"] == "result_object_missing"


async def test_a_started_call_has_no_result_and_says_why(world: World) -> None:
    call = await world.tool_call(1, complete=False)
    detail = await world.reader.get_tool_call(world.eid, call)
    assert detail.status == "started" and detail.result is None
    assert detail.integrity == "no_result" and "may or may not" in (
        detail.result_missing_reason or ""
    )


async def test_a_foreign_or_unknown_tool_call_is_not_found(
    world: World, session_factory, artifact_store
) -> None:
    other = await make_world(session_factory, artifact_store)
    foreign = await other.tool_call(1)
    await _expect(
        world.reader.get_tool_call(world.eid, foreign), OperationErrorCode.TOOL_CALL_NOT_FOUND
    )
    await _expect(
        world.reader.get_tool_call(world.eid, str(uuid.uuid4())),
        OperationErrorCode.TOOL_CALL_NOT_FOUND,
    )


# --------------------------------------------------------------------------- trace


async def _full_chain(world: World) -> dict[str, str]:
    """全ての鎖がある Episode（tool → 判断 → 戦略 → 演出 → 絵コンテ → 品質 → 投稿 → Lesson）。"""
    trend = await world.tool_call(1)
    stats = await world.tool_call(2, tool=ToolId.GET_VIDEO_ANALYTICS)
    strategy = await world.artifact(world.strategy(citations=(trend,)))
    decision = await world.decision("strategy_brief", revision=strategy, cited=[trend, stats])
    creative = await world.artifact(world.creative())
    await world.link(strategy, creative)
    storyboard_v1 = await world.raw(ArtifactType.STORYBOARD, {"v": 1})
    storyboard_v2 = await world.raw(ArtifactType.STORYBOARD, {"v": 2})  # 人の修正後の版
    await world.link(creative, storyboard_v1)
    await world.link(storyboard_v1, storyboard_v2)
    review = await world.artifact(world.review())
    await world.link(creative, review)
    receipt = await world.artifact(world.receipt())
    lesson = await world.artifact(world.lesson(measured=True))
    return {
        "trend": trend,
        "stats": stats,
        "strategy": strategy,
        "decision": decision,
        "creative": creative,
        "review": review,
        "sb1": storyboard_v1,
        "sb2": storyboard_v2,
        "receipt": receipt,
        "lesson": lesson,
    }


async def test_a_full_chain_answers_why_what_reflected_result_and_next(world: World) -> None:
    ids = await _full_chain(world)
    trace = await world.reader.improvement_trace(world.eid)
    assert trace.missing == {}  # 鎖が全部ある
    # なぜこの題材か: 判断 + 引用した tool 呼び出し
    assert trace.topic_selection is not None
    (decision,) = trace.topic_selection.decisions
    assert decision.decision_id == ids["decision"] and decision.rationale_kind == "reported"
    assert set(decision.cited_tool_call_ids) == {ids["trend"], ids["stats"]}
    assert trace.topic_selection.strategy_brief is not None
    assert trace.topic_selection.strategy_brief.artifact_id == ids["strategy"]
    assert trace.topic_selection.audience_basis == "assumed"  # 仮定を実測と言わない
    cited = {c.tool_call_id: c.cited_by for c in trace.tool_calls}
    assert f"decision:{ids['decision']}" in cited[ids["trend"]]
    assert f"strategy_brief:{ids['strategy']}" in cited[ids["trend"]]
    assert trace.unresolved_citations == ()
    # 何を良くするつもりか
    assert trace.intended_improvement is not None
    assert trace.intended_improvement.success_criteria == ("average view percentage above 70%",)
    assert trace.intended_improvement.retention_hypotheses == ("open on the paradox",)
    # 動画への反映（戦略 -> 演出 -> 品質 の derived_from）
    assert trace.reflected_in_video is not None
    assert trace.reflected_in_video.creative_brief is not None
    assert trace.reflected_in_video.creative_brief.derived_from == (ids["strategy"],)
    derived = {a.artifact_id for a in trace.reflected_in_video.derived_artifacts}
    assert derived == {ids["sb1"], ids["sb2"], ids["review"]}  # 演出指針の下流（本体は含めない）
    revisions = trace.reflected_in_video.storyboard_revisions
    assert [a.artifact_id for a in revisions] == [ids["sb1"], ids["sb2"]]  # 修正の履歴（古い順）
    assert [a.is_current for a in revisions] == [False, True]
    assert revisions[1].derived_from == (ids["sb1"],)
    assert trace.quality_review is not None and trace.quality_review.overall == "pass"
    # 投稿・結果・次の提案
    assert trace.publication is not None and trace.publication.video_id == "abcdefghijk"
    assert trace.publication.privacy_status == "private"
    assert trace.result.status == "measured" and trace.result.lesson_status == "proposed"
    assert trace.result.measurement_windows[0]["views"] == 1200
    assert trace.next_proposal is not None
    assert trace.next_proposal.proposed_experiments == ("open on the paradox in 3s",)
    assert trace.next_proposal.lesson_status == "proposed"  # 仮説であって検証済みではない


async def test_the_trace_is_deterministic_and_machine_readable(world: World) -> None:
    await _full_chain(world)
    first = await world.reader.improvement_trace(world.eid)
    second = await world.reader.improvement_trace(world.eid)
    assert first == second
    assert ImprovementTrace.model_validate_json(first.model_dump_json()) == first


async def test_an_empty_episode_names_every_missing_link_with_a_reason(world: World) -> None:
    trace = await world.reader.improvement_trace(world.eid)
    assert set(trace.missing) == set(TRACE_LINKS)
    assert all(trace.missing[name].strip() for name in TRACE_LINKS)
    assert trace.tool_calls == () and trace.topic_selection is None
    assert trace.publication is None and trace.next_proposal is None
    assert trace.result == TraceResult(
        status="not_published", reason="no upload receipt: the episode is not published"
    )


@pytest.mark.parametrize(
    ("removed", "expected_missing"),
    [
        (ArtifactType.STRATEGY_BRIEF, {"topic_selection"}),
        (ArtifactType.CREATIVE_BRIEF, {"reflected_in_video"}),
        (ArtifactType.QUALITY_REVIEW, {"quality_review"}),
        (ArtifactType.UPLOAD_RECEIPT, {"publication"}),
        (ArtifactType.LESSON, {"next_proposal"}),
    ],
)
async def test_each_missing_link_is_explicit_and_the_rest_stay(
    world: World, removed: ArtifactType, expected_missing: set[str]
) -> None:
    ids = await _full_chain(world)
    async with world.session_factory() as s:
        await s.execute(
            update(ArtifactMetadataRow)
            .where(ArtifactMetadataRow.artifact_type == removed.value)
            .values(superseded_at=ArtifactMetadataRow.created_at)  # 現行ではなくす
        )
        await s.commit()
    trace = await world.reader.improvement_trace(world.eid)
    assert set(trace.missing) == expected_missing, trace.missing
    for reason in trace.missing.values():
        assert reason.strip()
    del ids
    if removed is ArtifactType.UPLOAD_RECEIPT:
        # 受領が無くても Lesson の測定は結果に出る（受領の欠落は publication に残る）
        assert trace.result.lesson is not None
    if removed is ArtifactType.LESSON:
        assert trace.result.status == "awaiting_measurement"
        assert "lesson" in trace.result.reason


async def test_a_lesson_without_any_measured_metric_is_awaiting_not_measured(world: World) -> None:
    await world.artifact(world.receipt())
    await world.artifact(world.lesson(measured=False))
    trace = await world.reader.improvement_trace(world.eid)
    assert trace.result.status == "awaiting_measurement"
    assert trace.result.measurement_windows[0]["views"] is None  # 0 で埋めない
    assert trace.result.measurement_windows[0]["missing_reason"] == "analytics not available yet"


async def test_a_published_episode_with_no_lesson_is_awaiting_measurement(world: World) -> None:
    await world.artifact(world.receipt())
    trace = await world.reader.improvement_trace(world.eid)
    assert trace.result.status == "awaiting_measurement" and trace.result.lesson is None
    assert trace.publication is not None


async def test_a_citation_without_a_tool_call_row_is_listed_as_unresolved(world: World) -> None:
    ghost = str(uuid.uuid4())
    strategy = await world.artifact(world.strategy(citations=(ghost,)))
    await world.decision("strategy_brief", revision=strategy, cited=[ghost])
    trace = await world.reader.improvement_trace(world.eid)
    assert trace.unresolved_citations == (f"tool_call:{ghost}",)


async def test_human_flags_on_calls_and_decisions_surface_in_the_trace(world: World) -> None:
    ids = await _full_chain(world)
    await _flag(world, f"tool_call:{ids['trend']}", "wrong_evidence")
    await _flag(world, f"decision:{ids['decision']}", "wrong_decision")
    trace = await world.reader.improvement_trace(world.eid)
    flags = sorted(a["payload"]["flag"] for a in trace.human_annotations)
    assert flags == ["wrong_decision", "wrong_evidence"]
    call = next(c for c in trace.tool_calls if c.tool_call_id == ids["trend"])
    assert [a["payload"]["flag"] for a in call.annotations] == ["wrong_evidence"]
    assert trace.topic_selection is not None
    (decision,) = trace.topic_selection.decisions
    assert [a["payload"]["flag"] for a in decision.annotations] == ["wrong_decision"]


async def test_tool_flags_are_not_quality_review_findings(
    world: World, session_factory, artifact_store
) -> None:
    from infrastructure.application.episode_reader import StoreBackedEpisodeReader

    ids = await _full_chain(world)
    await _flag(world, f"tool_call:{ids['trend']}", "wrong_evidence")
    result = await StoreBackedEpisodeReader(session_factory, lambda: artifact_store).quality_review(
        world.eid
    )
    assert result.findings == ()


# --------------------------------------------------------------------------- read-only / secrets


async def test_none_of_the_reads_writes_anything(world: World) -> None:
    ids = await _full_chain(world)
    await _flag(world, f"tool_call:{ids['trend']}", "wrong_evidence")
    before = await world.counts()
    writes_before = {k: world.store.write_count(k) for k in list(world.store._objects)}  # noqa: SLF001
    await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    await world.reader.get_tool_call(world.eid, ids["trend"])
    await world.reader.improvement_trace(world.eid)
    assert await world.counts() == before
    assert {k: world.store.write_count(k) for k in list(world.store._objects)} == writes_before  # noqa: SLF001


async def test_no_secret_or_result_body_leaks_into_list_or_trace(world: World) -> None:
    await world.tool_call(1, tool_input={"region": "JP", "authorization": SECRET})
    listing = (
        await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    ).model_dump_json()
    trace = (await world.reader.improvement_trace(world.eid)).model_dump_json()
    for blob in (listing, trace):
        assert SECRET not in blob and RESULT_BODY not in blob


async def test_the_row_status_vocabulary_is_what_the_summary_reports(world: World) -> None:
    call = await world.tool_call(1, status=ToolStatus.NO_DATA)
    async with world.session_factory() as s:
        row = await s.get(AgentToolCallRow, call)
    page = await world.reader.list_tool_calls(world.eid, ListToolCallsRequest())
    assert row is not None and page.calls[0].status == row.status == "no_data"
