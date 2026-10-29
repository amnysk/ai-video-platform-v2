"""Production の plan が、有料呼び出しの前に地図の指定を確かめる（ADR-0084 §7）。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.geo_scene import GeoMapSpec, GeoMapSpecs
from contracts.production_activities import ProductionPlanRequest
from domain.errors import GeoMapRefusedError, GeoSourceInvalidError
from infrastructure.geo.preflight import GeoPreflight
from infrastructure.geo.source import GeoSourceData
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support import geo_scene as gs
from tests.support.voice import BUCKET, create_episode, record_inputs
from workers.production.activities import ProductionActivities


class Source:
    def __init__(self, data: GeoSourceData | None = None, error: BaseException | None = None):
        self.data, self.error, self.calls = data, error, 0

    def load(self, episode_id: str) -> GeoSourceData | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.data


def data(*specs: GeoMapSpec) -> GeoSourceData:
    return GeoSourceData(
        specs=GeoMapSpecs(schema_version="1.0", specs=specs),
        gazetteer=gs.GAZETTEER,
        claims=gs.CLAIMS,
        sources=gs.SOURCES,
    )


def sb2(**over: Any) -> GeoMapSpec:
    return GeoMapSpec(storyboard_scene_id="sb2", request=gs.request(**over))  # sb2 は s2 の区間


async def plan(session_factory, source: Source | None):
    store = InMemoryArtifactStore()
    episode = await create_episode(session_factory)
    await record_inputs(session_factory, store, episode)
    acts = ProductionActivities(
        session_factory=session_factory,
        store=store,
        bucket=BUCKET,
        geo_preflight=GeoPreflight(source) if source is not None else None,
    )
    return await acts.plan(ProductionPlanRequest(episode, "episode-x-production", "run-1"))


async def test_flag_off_does_not_look_at_geo_at_all(session_factory) -> None:
    result = await plan(session_factory, None)
    assert [i.scene_id for i in result.images] == ["sb1", "sb2", "sb3", "sb4"]


async def test_no_specs_means_the_plan_is_unchanged(session_factory) -> None:
    off = await plan(session_factory, None)
    src = Source(None)
    on = await plan(session_factory, src)
    assert src.calls == 1
    assert [i.scene_id for i in on.images] == [i.scene_id for i in off.images]
    assert [v.requested_duration_ms for v in on.videos] == [
        v.requested_duration_ms for v in off.videos
    ]


async def test_a_good_map_leaves_the_plan_unchanged(session_factory) -> None:
    result = await plan(session_factory, Source(data(sb2())))
    assert len(result.images) == 4


@pytest.mark.parametrize(
    "over",
    [
        {"places": (gs.place_req("gz:edo-tokyo", "Tokyo"),)},  # 時代錯誤
        {"places": (gs.place_req(source_ids=("S-404",)),)},  # 出典が根拠に解決しない
        {"places": (gs.place_req(claim_id=None),)},  # 根拠なし
        {"as_of": "unknown"},  # 時間基準が不明
    ],
)
async def test_a_map_that_would_be_refused_stops_production_before_any_paid_call(
    session_factory, over: dict[str, Any]
) -> None:
    with pytest.raises(GeoMapRefusedError) as err:
        await plan(session_factory, Source(data(sb2(**over))))
    message = str(err.value)
    assert "s2:" in message and "Tokyo" not in message  # 理由コードだけ。入力の写しなし


async def test_a_spec_for_a_scene_outside_its_script_scene_is_an_input_error(
    session_factory,
) -> None:
    wrong = GeoMapSpec(storyboard_scene_id="sb1", request=gs.request())  # sb1 は s1
    with pytest.raises(GeoSourceInvalidError):
        await plan(session_factory, Source(data(wrong)))


async def test_an_unreadable_source_is_not_treated_as_no_map(session_factory) -> None:
    with pytest.raises(GeoSourceInvalidError):
        await plan(session_factory, Source(error=GeoSourceInvalidError("specs.json unreadable")))
