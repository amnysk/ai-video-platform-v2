"""BGM・地図の producer と、render の宣言を実際に読む品質ゲート（ADR-0086 / INV-146〜148）。

render（実の Activity + fake エンジン）→ 宣言・証跡 → ``QualityGateService``（実の producer）を
1 本につなぐ。SQLite + メモリストア。テストの理由は docs/testing/v3-phase3-wiring.md。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from contracts.quality import QualityReason, QualityVerdict
from contracts.quality_records import CheckKey, UsageFeature
from contracts.render_activities import RenderMarkReadyRequest
from contracts.upload import UPLOAD_DESCRIPTION_COMPOSITION_ENV
from domain.music.intelligibility import SceneSpeechLevels
from infrastructure.config import Settings
from infrastructure.db.models import QualityCheckRecordRow
from infrastructure.db.quality_repositories import SqlQualityRecordWriter
from infrastructure.geo.service import GeoRenderService
from infrastructure.music.credits import BgmCreditSource
from infrastructure.music.evidence import BgmEvidenceError
from infrastructure.quality.service import QualityGateService
from infrastructure.quality.wiring import build_quality_producers
from tests.support import geo_scene as gs
from tests.support.bgm_catalog import make_track, sha
from tests.support.bgm_render import CREDIT_TEXT, cue, sheet
from tests.support.geo_scene import FakeEncoder
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import WF, Harness
from tests.unit.test_render_activities_bgm import (
    Clock,
    FakeBgmSource,
    FakeLoudness,
    _admit_and_render,
)
from tests.unit.test_render_activities_geo import FakeGeoSource, data_for, sb2_spec
from tests.unit.test_render_usage_declarations import FakeSpeechProbe
from tests.unit.test_upload_activities import CHANNEL_ID

CREDIT = make_track(
    "bgm-credit", "f", attribution_required=True, exact_attribution_text=CREDIT_TEXT
)
GOOD = (SceneSpeechLevels("s1", -1600, -3000), SceneSpeechLevels("s2", -1600, -3000))
TOO_LOUD = (SceneSpeechLevels("s1", -1600, -1700), SceneSpeechLevels("s2", -1600, -1700))


def _write_catalog(root: Path, *tracks: Any) -> None:
    (root / "bgm").mkdir(parents=True, exist_ok=True)
    payload = {"tracks": [t.model_dump(mode="json") for t in tracks]}
    (root / "bgm" / "catalog.json").write_text(json.dumps(payload), encoding="utf-8")


class World:
    """1 本の Episode を render し、その結果を gate で評価する。"""

    def __init__(self, session_factory, store, tmp_path: Path) -> None:
        self.factory = session_factory
        self.store = store
        self.tmp = tmp_path
        self.clock = Clock()
        self.seed: Any = None
        self.render: Harness | None = None
        _write_catalog(tmp_path, CREDIT)

    async def render_with(
        self,
        *,
        bgm: bool = True,
        levels: tuple[SceneSpeechLevels, ...] | Exception | None = GOOD,
        geo: Any = None,
        writer: bool = True,
        run: str = "run-1",
    ):
        if self.seed is None:
            self.seed = await seed_render_inputs(self.factory, self.store)
        extra: dict[str, Any] = {"clock": self.clock}
        if bgm:
            cues = sheet(
                cue(track_id="bgm-credit", source_sha256=sha("f"), credits_text=CREDIT_TEXT)
            )
            extra["bgm_source"] = FakeBgmSource(self.tmp, (CREDIT,), cues)
            extra["loudness_probe"] = FakeLoudness()
            if isinstance(levels, Exception):
                extra["speech_probe"] = FakeSpeechProbe(error=levels)
            elif levels is not None:
                extra["speech_probe"] = FakeSpeechProbe(levels)
        if geo is not None:
            extra["geo"] = GeoRenderService(source=geo, encoder=FakeEncoder(), font_path=None)
        if writer:
            extra["quality_writer"] = SqlQualityRecordWriter(self.factory)
        workdir = self.tmp / f"render-{run}"
        workdir.mkdir(parents=True, exist_ok=True)
        self.render = Harness(self.factory, self.store, workdir, **extra)
        result = await _admit_and_render(self.render, self.seed, run=run)
        assert not result.skipped
        return result

    def gate(self, **kwargs: Any) -> QualityGateService:
        settings = Settings(_env_file=None, ai_video_work_root=str(self.tmp))  # type: ignore[call-arg]
        return QualityGateService(
            session_factory=self.factory,
            store=self.store,
            producers=build_quality_producers(settings, self.store),
            destination_id=CHANNEL_ID,
            clock=self.clock,
            **kwargs,
        )

    async def evaluate(self, **kwargs: Any):
        return await self.gate(**kwargs).evaluate(self.seed.episode_id, manual=False)

    async def record_keys(self) -> set[str]:
        async with self.factory() as s:
            rows = (await s.execute(select(QualityCheckRecordRow))).scalars().all()
        return {r.check_key for r in rows}

    def evidence_keys(self, kind: str) -> list[str]:
        return [k for k in self.store._objects if f"/{kind}/" in k]  # type: ignore[attr-defined]


@pytest.fixture
def world(session_factory, artifact_store, tmp_path) -> World:
    return World(session_factory, artifact_store, tmp_path)


def _good_map() -> FakeGeoSource:
    return FakeGeoSource(data_for(sb2_spec()))


def _refused_map() -> FakeGeoSource:
    return FakeGeoSource(data_for(sb2_spec(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),))))


# --- 通る ------------------------------------------------------------------------------


async def test_with_everything_on_and_fresh_records_the_episode_is_admitted(world: World) -> None:
    await world.render_with(geo=_good_map())
    outcome = await world.evaluate()
    assert outcome.assessment.decision.allowed, outcome.assessment
    assert outcome.assessment.verdict is QualityVerdict.PASS
    assert await world.record_keys() == {"technical_qa", "description_lint", "bgm", "map"}


async def test_a_video_without_bgm_or_map_is_admitted_by_the_declared_non_use(
    world: World,
) -> None:
    await world.render_with(bgm=False)
    outcome = await world.evaluate()
    assert outcome.assessment.decision.allowed, outcome.assessment
    assert await world.record_keys() == {"technical_qa", "description_lint"}


# --- 止まる ----------------------------------------------------------------------------


async def test_bgm_used_but_its_check_cannot_be_recorded_is_review(world: World) -> None:
    await world.render_with()
    for key in world.evidence_keys("bgm_render_evidence"):
        del world.store._objects[key]  # type: ignore[attr-defined]
    outcome = await world.evaluate()
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.REVIEW
    assert "bgm" not in await world.record_keys()


async def test_a_missing_catalog_means_no_bgm_record_and_review(world: World) -> None:
    await world.render_with()
    (world.tmp / "bgm" / "catalog.json").unlink()
    outcome = await world.evaluate()
    assert outcome.assessment.verdict is QualityVerdict.REVIEW
    assert "bgm" not in await world.record_keys()


async def test_a_license_revoked_after_rendering_is_blocked(world: World) -> None:
    await world.render_with()
    revoked = make_track(
        "bgm-credit",
        "f",
        attribution_required=True,
        exact_attribution_text=CREDIT_TEXT,
        license_status="expired",
    )
    _write_catalog(world.tmp, revoked)
    outcome = await world.evaluate()
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.BLOCK
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_BLOCKED


async def test_an_unmeasured_speech_margin_is_review_not_pass(world: World) -> None:
    from domain.errors import RenderEngineFailedError

    await world.render_with(levels=RenderEngineFailedError("x"))
    outcome = await world.evaluate()
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.REVIEW


async def test_narration_buried_under_bgm_is_blocked(world: World) -> None:
    await world.render_with(levels=TOO_LOUD)
    outcome = await world.evaluate()
    assert outcome.assessment.verdict is QualityVerdict.BLOCK
    assert not outcome.assessment.decision.allowed


async def test_a_refused_map_stops_the_upload(world: World) -> None:
    await world.render_with(bgm=False, geo=_refused_map())
    outcome = await world.evaluate()
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.BLOCK
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_BLOCKED


async def test_a_declared_map_whose_evidence_is_gone_is_review(world: World) -> None:
    await world.render_with(bgm=False, geo=_good_map())
    for key in world.evidence_keys("geo_map_evidence"):
        del world.store._objects[key]  # type: ignore[attr-defined]
    outcome = await world.evaluate()
    assert outcome.assessment.verdict is QualityVerdict.REVIEW
    assert "map" not in await world.record_keys()


async def test_a_re_rendered_video_does_not_inherit_the_old_declarations_or_records(
    world: World,
) -> None:
    await world.render_with(geo=_good_map())
    first = await world.evaluate()
    assert first.assessment.decision.allowed
    # 入力が変わる再描画（BGM を外す）。宣言は書かれない（ENFORCE を外した状態の模擬）
    await world.render_with(bgm=False, writer=False, run="run-2")
    second = await world.evaluate()
    assert second.final_video_sha256 != first.final_video_sha256
    assert not second.assessment.decision.allowed
    assert second.assessment.verdict is QualityVerdict.REVIEW


async def test_the_gate_reads_the_declaration_the_render_wrote(world: World) -> None:
    await world.render_with()
    outcome = await world.evaluate()
    async with world.factory() as s:
        from domain.quality.records import CheckSubject
        from infrastructure.db.quality_repositories import QualityRecordRepository

        current = await QualityRecordRepository(s).current_final_video(world.seed.episode_id)
        assert current is not None
        subject = CheckSubject(
            world.seed.episode_id, current.final_video_sha256, current.input_hash
        )
        declared = {d.feature: d for d in await QualityRecordRepository(s).list_usage(subject)}
    assert declared[UsageFeature.BGM].used and not declared[UsageFeature.MAP].used
    assert outcome.final_video_sha256 == current.final_video_sha256


def test_both_branch_producers_are_registered(tmp_path: Path) -> None:
    from infrastructure.storage.memory_store import InMemoryArtifactStore

    settings = Settings(_env_file=None, ai_video_work_root=str(tmp_path))  # type: ignore[call-arg]
    producers = build_quality_producers(settings, InMemoryArtifactStore())
    assert [p.key for p in producers] == [CheckKey.BGM, CheckKey.MAP]


# --- クレジット（INV-148）---------------------------------------------------------------


async def test_credit_lines_are_not_read_while_the_description_flag_is_off(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, raising=False)
    await world.render_with()

    class Exploding:
        async def get_json(self, key: str):  # pragma: no cover - 呼ばれたら失敗
            raise AssertionError("the store must not be touched")

    source = BgmCreditSource(Exploding(), world.factory)  # type: ignore[arg-type]
    assert await source.credit_lines("x", final_video_sha256=sha("1"), media_sha256=sha("2")) == ()


async def test_credit_lines_come_from_the_evidence_when_the_flag_is_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "true")
    await world.render_with()
    lines = await _credits(world)
    assert lines == (CREDIT_TEXT,)


async def test_no_bgm_means_no_credit_lines(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "true")
    await world.render_with(bgm=False)
    assert await _credits(world) == ()


async def test_an_unreadable_evidence_is_an_error_not_an_empty_credit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "true")
    await world.render_with()
    (key,) = world.evidence_keys("bgm_render_evidence")
    world.store._objects[key] = b"{not json"  # type: ignore[attr-defined]
    with pytest.raises(BgmEvidenceError):
        await _credits(world)


async def _credits(world: World) -> tuple[str, ...]:
    from contracts.artifacts import parse_final_video
    from infrastructure.db.quality_repositories import QualityRecordRepository

    async with world.factory() as s:
        current = await QualityRecordRepository(s).current_final_video(world.seed.episode_id)
    assert current is not None
    final = parse_final_video(await world.store.get_json(current.object_key))
    return await BgmCreditSource(world.store, world.factory).credit_lines(
        world.seed.episode_id,
        final_video_sha256=current.final_video_sha256,
        media_sha256=final.media.sha256,
    )


async def test_the_linted_description_equals_the_uploaded_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gate が lint する説明欄と、upload が YouTube へ送る説明欄が、BGM クレジットを含めて同一。"""
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "true")
    await world.render_with()
    await world.render.activities.mark_ready(  # type: ignore[union-attr]
        RenderMarkReadyRequest(world.seed.episode_id, WF, "run-1")
    )
    linted: list[str] = []
    import infrastructure.quality.service as svc

    real = svc.build_upload_metadata

    def spy(**kw: Any):
        metadata = real(**kw)
        linted.append(metadata.description)
        return metadata

    monkeypatch.setattr(svc, "build_upload_metadata", spy)
    credit_source = BgmCreditSource(world.store, world.factory)
    outcome = await world.evaluate(credit_source=credit_source)
    assert outcome.assessment.decision.allowed, outcome.assessment

    from tests.unit.test_upload_activities import Harness as UploadHarness

    uh = UploadHarness(world.factory, world.store, world.tmp / "upload")
    uh.episode_id = world.seed.episode_id
    acts = uh.activities(credit_source=credit_source)
    await uh.admit(acts)
    await uh.upload(acts)
    (session,) = uh.fake.sessions.values()
    sent = session.metadata["snippet"]["description"]

    assert CREDIT_TEXT in sent
    assert linted and linted[-1] == sent
