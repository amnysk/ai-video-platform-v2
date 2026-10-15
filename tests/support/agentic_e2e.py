"""JP-MAS-002 P1-D の実環境 E2E 用の支援（PostgreSQL + Temporal + MinIO の隔離スタック）。

- ``CANNED``: 4 role の LLM 応答（``FakeAgentLlm`` の responder。ネットワークに出ない）
- ``MockPublicMetrics``: ``PublicMetricsPort`` の mock（公開前 / 公開後を切り替える）
- ``FakeStageActivities`` と ``Fake*`` workflow: 既存工程の子 workflow の偽物。次の工程が読む
  最小の現行 Artifact と Episode の状態だけを書く（再実行しても増やさない）

各テストの理由: docs/testing/jpmas2-p1d-rationale.md
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow

from contracts.artifacts import (
    build_script_artifact,
    build_storyboard_artifact,
)
from contracts.render_activities import (
    RenderAdmitRequest,
    RenderFinalVideoRequest,
    RenderMarkReadyRequest,
)
from contracts.states import ArtifactType, EpisodeStatus
from contracts.upload import build_upload_receipt, upload_marker
from domain.artifact.hashing import sha256_hex
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention.records import WRITER_DIRECTOR_ACTOR
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.production import SCRIPT_SCENES
from tests.support.render_activity import (
    PassingSourceProbe,
    RenderSeed,
    record_artifact,
    record_manifest,
    record_scene_video,
    record_scene_voice,
)
from tests.support.voice import STORYBOARD_LAYOUT
from workers.render.activities import RenderActivities

BUCKET = "artifacts"
FAKE_STAGE_ACTIVITY = "e2e_fake_stage"
CHANNEL_ID = "UC" + "a" * 22
VIDEO_ID = "abcdefghijk"

#: 子 workflow 名 -> 返す status（``STAGE_PARKING_STATUS`` と同じ）
STAGE_STATUS = {
    "ScriptWorkflow": "script_ready",
    "StoryboardWorkflow": "storyboard_ready",
    "ProductionWorkflow": "assets_ready",
    "RenderWorkflow": "render_ready",
    "UploadWorkflow": "uploaded",
}

# --------------------------------------------------------------------------- LLM の応答

STRATEGY_TITLE = "Why the Shogun hid in plain sight"
QA_PROPOSED_VISUAL = "A clear aerial view of the castle town"


def canned(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    """schema の title で 4 role の応答を返す。知らない schema は ``LookupError``（黙らない）。"""
    title = schema.get("title")
    if title == "StrategyResponse":
        return {
            "audience_hypothesis": "US 20s like power-structure stories.",
            "candidates": [
                {
                    "id": "c1",
                    "title": STRATEGY_TITLE,
                    "subject": "tokugawa_ieyasu",
                    "angle": "reason",
                    "era": "edo",
                    "hook": "He ruled Japan without ever being king.",
                    "rationale": "Strong curiosity gap.",
                }
            ],
            "recommended_candidate_id": "c1",
        }
    if title == "CreativeResponse":
        return {"hook_options": ["He ruled Japan without ever being king."], "tone": "dry wit"}
    if title == "QualityResponse":
        return {
            "findings": [
                {
                    "id": "f1",
                    "severity": "major",
                    "category": "visual",
                    "scene_id": "sb1",
                    "time_range": {"start_ms": 0, "end_ms": 1500},
                    "evidence": "The map is unreadable.",
                    "confidence": 0.7,
                    "recommended_visual_description": QA_PROPOSED_VISUAL,
                },
                {
                    "id": "f2",
                    "severity": "minor",
                    "category": "pacing",
                    "scene_id": "sb2",
                    "time_range": {"start_ms": 8000, "end_ms": 9000},
                    "evidence": "The cut is a little abrupt.",
                    "confidence": 0.5,
                },
            ],
            "rationale": "Self-assessed from the rendered video and the storyboard.",
        }
    if title == "LessonResponse":
        return {
            "observations": ["The 48h window shows 80 views."],
            "hypotheses": ["The hook may have helped retention."],
            "alternative_explanations": ["The release time may explain the views."],
            "proposed_experiments": ["Try a second hook on a comparable video."],
        }
    raise LookupError(f"no canned response for schema {title!r}")


class MockPublicMetrics:
    """``PublicMetricsPort`` の mock。``public`` を切り替えて公開前 / 公開後を作る。"""

    def __init__(self) -> None:
        self.public = False
        self.calls: list[str | None] = []

    async def measurements(self, *, episode_id: str, video_id: str | None) -> dict[str, Any]:
        self.calls.append(video_id)
        if not self.public:
            return {"public": False, "windows": [], "reason": "mock_not_public"}
        fetched = datetime(2026, 10, 20, tzinfo=UTC)
        return {
            "public": True,
            "published_at": (fetched - timedelta(days=2)).isoformat(),
            "format": "shorts",
            "windows": [
                {
                    "window": "48h",
                    "fetched_at": fetched.isoformat(),
                    "views": 80,
                    "subscribers_gained": 3,
                    "average_view_duration_seconds": 22.5,
                    "average_view_percentage": 61.0,
                }
            ],
        }


# --------------------------------------------------------------------------- 既存工程の偽物


class FakeStageActivities:
    """script / storyboard / production / render / upload の子 workflow の中身。

    DB と ArtifactStore に、次の工程が読む現行 Artifact と Episode の状態だけを書く。
    render は本物の ``RenderActivities``（fake engine）で final_video を作る。
    すでに書いた Artifact は再び書かない（再実行で現行 revision を増やさない）。
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore, tmp: Path
    ) -> None:
        self._sf = session_factory
        self._store = store
        self._tmp = tmp
        #: 実行した工程（episode_id, 工程名）
        self.ran: list[tuple[str, str]] = []

    async def _current(self, episode_id: str, artifact_type: ArtifactType):
        async with self._sf() as session:
            return await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, artifact_type
            )

    async def _events(self, episode_id: str, *events: EpisodeEvent) -> None:
        async with self._sf() as session:
            repo = EpisodeRepository(session)
            for event in events:
                await repo.apply_event(episode_id, event)
            await session.commit()

    async def _status(self, episode_id: str) -> EpisodeStatus:
        async with self._sf() as session:
            episode = await EpisodeRepository(session).get(episode_id)
        assert episode is not None
        return episode.status

    async def _decided(self, episode_id: str, decision_type: str, revision_id: str) -> None:
        """本物の script / storyboard 工程が残す生成記録の代わり（INV-46: 記録の無い現行 revision
        があると、auto_intervenable の Episode は有料 dispatch を拒否される）。"""
        async with self._sf() as session:
            await InterventionStore(session).insert_decision(
                idempotency_key=f"e2e-{decision_type}:{revision_id}",
                decision_type=decision_type,
                actor=WRITER_DIRECTOR_ACTOR,
                payload={},
                episode_id=episode_id,
                result_revision_id=revision_id,
            )
            await session.commit()

    @activity.defn(name=FAKE_STAGE_ACTIVITY)
    async def run(self, stage: str, episode_id: str) -> None:
        self.ran.append((episode_id, stage))
        await getattr(self, f"_{stage}")(episode_id)

    async def _ScriptWorkflow(self, ep: str) -> None:  # noqa: N802
        if await self._current(ep, ArtifactType.SCRIPT) is not None:
            return
        if await self._status(ep) is EpisodeStatus.PLANNED:
            await self._events(ep, EpisodeEvent.WORKFLOW_STARTED)
        script = await record_artifact(
            self._sf,
            self._store,
            ep,
            ArtifactType.SCRIPT,
            build_script_artifact(
                episode_id=ep,
                language="ja",
                title="縄文の食",
                hook="土器が語る",
                scenes=list(SCRIPT_SCENES),
                metadata={"topic": "e2e", "generator": "fake", "generator_model": "fake"},
            ),
        )
        await self._decided(ep, "script", script.id)
        await self._events(ep, EpisodeEvent.SCRIPT_READY)

    async def _StoryboardWorkflow(self, ep: str) -> None:  # noqa: N802
        if await self._current(ep, ArtifactType.STORYBOARD) is not None:
            return
        script = await self._current(ep, ArtifactType.SCRIPT)
        assert script is not None, "the storyboard stage ran before the script stage"
        await self._events(ep, EpisodeEvent.STAGE_ADMITTED)
        storyboard = await record_artifact(
            self._sf,
            self._store,
            ep,
            ArtifactType.STORYBOARD,
            build_storyboard_artifact(
                episode_id=ep,
                source_script={
                    "artifact_id": script.id,
                    "sha256": script.sha256,
                    "schema_version": script.schema_version,
                },
                scenes=[
                    {
                        "scene_id": f"sb{i}",
                        "order": i,
                        "script_scene_id": sid,
                        "start_ms": start,
                        "duration_ms": duration,
                        "visual_kind": kind,
                        "visual_description": desc,
                    }
                    for i, (sid, start, duration, kind, desc) in enumerate(
                        STORYBOARD_LAYOUT, start=1
                    )
                ],
                total_duration_ms=25000,
                metadata={
                    "generator": "fake",
                    "generator_model": "fake",
                    "generation_spec_id": "spec-1",
                },
            ),
        )
        await self._decided(ep, "storyboard", storyboard.id)
        await self._events(ep, EpisodeEvent.STORYBOARD_READY)

    async def _ProductionWorkflow(self, ep: str) -> None:  # noqa: N802
        if await self._current(ep, ArtifactType.PRODUCTION_MANIFEST) is not None:
            return
        script = await self._current(ep, ArtifactType.SCRIPT)
        storyboard = await self._current(ep, ArtifactType.STORYBOARD)
        assert script is not None and storyboard is not None
        await self._events(ep, EpisodeEvent.STAGE_ADMITTED)
        seed = RenderSeed(episode_id=ep, script=script, storyboard=storyboard)
        by_script: dict[str, list[str]] = {}
        for index, (script_scene_id, _start, duration, _kind, _desc) in enumerate(
            STORYBOARD_LAYOUT, start=1
        ):
            await record_scene_video(self._sf, self._store, seed, f"sb{index}", duration)
            by_script.setdefault(script_scene_id, []).append(f"sb{index}")
        for script_scene_id, scene_ids in by_script.items():
            await record_scene_voice(self._sf, self._store, seed, script_scene_id, scene_ids)
        await record_manifest(self._sf, self._store, seed)
        await self._events(ep, EpisodeEvent.ASSETS_READY)

    async def _RenderWorkflow(self, ep: str) -> None:  # noqa: N802
        if await self._current(ep, ArtifactType.FINAL_VIDEO) is not None:
            return
        font = self._tmp / "e2e-font.ttc"
        font.write_bytes(b"e2e font")
        probe = FakeFinalVideoProbe()
        engine = FakeRenderEngine(
            payload=b"final-video-bytes-for-e2e",
            on_render=lambda r: setattr(probe, "plan", r.plan),
        )
        render = RenderActivities(
            session_factory=self._sf,
            store=self._store,
            bucket=BUCKET,
            workdir=WorkDirectory(self._tmp / "render-work", forbidden=()),
            engine=engine,
            probe=probe,
            source_probe=PassingSourceProbe(),
            font_path=font,
            font_sha256=sha256_hex(b"e2e font"),
            render_timeout_seconds=60,
            min_free_bytes=0,
            heartbeat=lambda *_: None,
        )
        wf = f"episode-{ep}-render"
        admitted = await render.admit(RenderAdmitRequest(ep, wf, "e2e-run"))
        assert admitted.admitted, admitted
        await render.render_final_video(
            RenderFinalVideoRequest(ep, wf, "e2e-run", "shorts_vertical")
        )
        ready = await render.mark_ready(RenderMarkReadyRequest(ep, wf, "e2e-run"))
        assert ready.status == "render_ready", ready

    async def _UploadWorkflow(self, ep: str) -> None:  # noqa: N802
        if await self._current(ep, ArtifactType.UPLOAD_RECEIPT) is not None:
            return
        final = await self._current(ep, ArtifactType.FINAL_VIDEO)
        assert final is not None
        key = "ab" * 32
        await self._events(ep, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.UPLOAD_SUCCEEDED)
        await record_artifact(
            self._sf,
            self._store,
            ep,
            ArtifactType.UPLOAD_RECEIPT,
            build_upload_receipt(
                episode_id=ep,
                source_final_video={
                    "artifact_id": final.id,
                    "sha256": final.sha256,
                    "schema_version": final.schema_version,
                },
                destination={"platform": "youtube", "channel_id": CHANNEL_ID},
                video_id=VIDEO_ID,
                metadata={
                    "title": "t",
                    "description": "d",
                    "tags": (upload_marker(key),),
                    "category_id": "22",
                    "default_language": "ja",
                    "privacy_status": "private",
                    "self_declared_made_for_kids": False,
                    "contains_synthetic_media": True,
                    "notify_subscribers": False,
                },
                upload_key=key,
                bytes=1234,
                reconciled_by="upload_response",
            ),
        )


async def _stage(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    await workflow.execute_activity(
        FAKE_STAGE_ACTIVITY,
        args=[name, payload["episode_id"]],
        task_queue=workflow.info().task_queue,
        start_to_close_timeout=timedelta(seconds=120),
    )
    return {"episode_id": payload["episode_id"], "status": STAGE_STATUS[name]}


@workflow.defn(name="ScriptWorkflow")
class FakeScript:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await _stage("ScriptWorkflow", payload)


@workflow.defn(name="StoryboardWorkflow")
class FakeStoryboard:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await _stage("StoryboardWorkflow", payload)


@workflow.defn(name="ProductionWorkflow")
class FakeProduction:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await _stage("ProductionWorkflow", payload)


@workflow.defn(name="RenderWorkflow")
class FakeRender:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await _stage("RenderWorkflow", payload)


@workflow.defn(name="UploadWorkflow")
class FakeUpload:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await _stage("UploadWorkflow", payload)


FAKE_STAGE_WORKFLOWS = [FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload]
