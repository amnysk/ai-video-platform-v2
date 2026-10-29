"""Phase 3 independent review (Sonnet E) evidence: the quality gate is not wired to BGM / geo.

These tests failed at 7fd36e4 and pass with the ADR-0086 wiring. They show review findings B1/B2 in
docs/architecture/v3-phase3-review.md: the producers (``bgm_quality_checks`` /
``map_quality_checks``) are never registered in the production gate, and Render never writes the
usage declarations that the gate requires. With AVP_QUALITY_GATE_ENFORCE on, every episode is
therefore REVIEW (``usage_declaration_missing``) and a refused map is invisible to the gate.
"""

from __future__ import annotations

from contracts.quality_records import CheckKey
from domain.quality.records import CheckSubject
from infrastructure.config import Settings
from infrastructure.db.quality_repositories import QualityRecordRepository, SqlQualityRecordWriter
from infrastructure.quality.wiring import build_quality_producers
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.geo_scene import FakeEncoder
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import _final_rows
from tests.unit.test_render_activities_bgm import _input_hashes
from tests.unit.test_render_activities_geo import (
    FakeGeoSource,
    admit_and_render,
    data_for,
    harness,
    sb2_spec,
)


def test_review_production_gate_has_bgm_and_map_producers() -> None:
    settings = Settings(_env_file=None, ai_video_work_root="/nonexistent")  # type: ignore[call-arg]
    keys = [p.key for p in build_quality_producers(settings, InMemoryArtifactStore())]
    # pins the exact registry: adding or dropping a producer must be a deliberate change here
    assert keys == [CheckKey.BGM, CheckKey.MAP], f"registered producers: {keys}"


async def test_review_render_declares_usage_for_the_gate(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    # the writer is passed only under AVP_QUALITY_GATE_ENFORCE (build_quality_record_writer)
    h = harness(
        session_factory,
        artifact_store,
        tmp_path,
        FakeGeoSource(data_for(sb2_spec())),
        FakeEncoder(),
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    await admit_and_render(h, seed)
    rows = await _final_rows(session_factory, seed.episode_id)
    subject = CheckSubject(
        seed.episode_id,
        rows[-1].sha256,
        (await _input_hashes(session_factory, seed.episode_id))[-1],
    )
    async with session_factory() as session:
        declared = await QualityRecordRepository(session).list_usage(subject)
    assert declared, "render wrote no usage declaration: the gate can only ever answer REVIEW"
