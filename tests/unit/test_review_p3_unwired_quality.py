"""Phase 3 independent review (Sonnet E) evidence: the quality gate is not wired to BGM / geo.

These tests are EXPECTED TO FAIL at 7fd36e4. They demonstrate review findings B1/B2 in
docs/architecture/v3-phase3-review.md: the producers (``bgm_quality_checks`` /
``map_quality_checks``) are never registered in the production gate, and Render never writes the
usage declarations that the gate requires. With AVP_QUALITY_GATE_ENFORCE on, every episode is
therefore REVIEW (``usage_declaration_missing``) and a refused map is invisible to the gate.
"""

from __future__ import annotations

from contracts.quality_records import CheckKey
from domain.quality.records import CheckSubject
from infrastructure.db.quality_repositories import QualityRecordRepository
from infrastructure.quality.wiring import build_quality_producers
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
    keys = {p.key for p in build_quality_producers()}
    assert {CheckKey.BGM, CheckKey.MAP} <= keys, f"registered producers: {sorted(keys)}"


async def test_review_render_declares_usage_for_the_gate(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = harness(
        session_factory,
        artifact_store,
        tmp_path,
        FakeGeoSource(data_for(sb2_spec())),
        FakeEncoder(),
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
