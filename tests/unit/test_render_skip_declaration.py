"""ENFORCE を ON にする前に描画された動画への使用宣言の補完（ADR-0086 追補 / INV-149）。

テストの理由は docs/testing/render-skip-declaration-rationale.md。SQLite + メモリストア + fake。
"""

from __future__ import annotations

from sqlalchemy import select

from infrastructure.db.models import QualityUsageDeclarationRow
from infrastructure.db.quality_repositories import SqlQualityRecordWriter
from infrastructure.geo.service import GeoRenderService
from tests.support.geo_scene import FakeEncoder
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import _final_rows
from tests.unit.test_render_activities_bgm import FakeLoudness, _admit_and_render, _input_hashes
from tests.unit.test_render_activities_geo import FakeGeoSource
from tests.unit.test_render_usage_declarations import (
    FailingWriter,
    _bgm_source,
    _declarations,
    _h,
)


async def _count(session_factory) -> int:
    async with session_factory() as s:
        return len((await s.execute(select(QualityUsageDeclarationRow))).scalars().all())


async def test_a_pre_enforce_render_gets_both_not_used_declarations_on_the_skip_path(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    before = _h(session_factory, artifact_store, tmp_path / "a")  # ENFORCE OFF = writer なし
    await _admit_and_render(before, seed)
    assert await _count(session_factory) == 0

    after = _h(
        session_factory,
        artifact_store,
        tmp_path / "b",
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    again = await _admit_and_render(after, seed, run="run-2")
    assert again.skipped and after.engine.requests == []  # 描画せず、動画の bytes は変わらない
    rows = await _declarations(session_factory)
    final = (await _final_rows(session_factory, seed.episode_id))[-1]
    input_hash = (await _input_hashes(session_factory, seed.episode_id))[-1]
    assert set(rows) == {"bgm", "map"}
    assert all(not r.used and r.evidence_ref is None for r in rows.values())
    assert all(r.final_video_sha256 == final.sha256 for r in rows.values())
    assert all(r.input_hash == input_hash for r in rows.values())
    assert len(await _final_rows(session_factory, seed.episode_id)) == 1


async def test_the_skip_path_declares_once_and_is_idempotent(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit_and_render(_h(session_factory, artifact_store, tmp_path / "a"), seed)
    for i in range(3):
        h = _h(
            session_factory,
            artifact_store,
            tmp_path / f"w{i}",
            quality_writer=SqlQualityRecordWriter(session_factory),
        )
        assert (await _admit_and_render(h, seed, run=f"run-{i + 2}")).skipped
    assert await _count(session_factory) == 2


async def test_the_skip_path_writes_nothing_while_enforce_is_off(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit_and_render(_h(session_factory, artifact_store, tmp_path / "a"), seed)
    again = await _admit_and_render(
        _h(session_factory, artifact_store, tmp_path / "b"), seed, run="run-2"
    )
    assert again.skipped
    assert await _count(session_factory) == 0


async def test_a_bgm_identity_in_the_hash_is_never_declared_not_used(
    session_factory, artifact_store, tmp_path
) -> None:
    """BGM が入力 hash にある動画は「使っていない」と言えない。宣言しない。"""
    seed = await seed_render_inputs(session_factory, artifact_store)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = _h(
        session_factory,
        artifact_store,
        tmp_path / "a",
        bgm_source=_bgm_source(tmp_path / "a"),
        loudness_probe=FakeLoudness(),
    )
    await _admit_and_render(first, seed)
    second = _h(
        session_factory,
        artifact_store,
        tmp_path / "b",
        bgm_source=_bgm_source(tmp_path / "b"),
        loudness_probe=FakeLoudness(),
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    again = await _admit_and_render(second, seed, run="run-2")
    assert again.skipped
    assert await _count(session_factory) == 0


async def test_a_geo_identity_in_the_hash_is_never_declared_not_used(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)

    def geo() -> GeoRenderService:
        return GeoRenderService(source=FakeGeoSource(None), encoder=FakeEncoder(), font_path=None)

    await _admit_and_render(_h(session_factory, artifact_store, tmp_path / "a", geo=geo()), seed)
    second = _h(
        session_factory,
        artifact_store,
        tmp_path / "b",
        geo=geo(),
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    again = await _admit_and_render(second, seed, run="run-2")
    assert again.skipped
    assert await _count(session_factory) == 0


async def test_a_failing_declaration_on_the_skip_path_does_not_fail_the_render(
    session_factory, artifact_store, tmp_path, caplog
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit_and_render(_h(session_factory, artifact_store, tmp_path / "a"), seed)
    h = _h(session_factory, artifact_store, tmp_path / "b", quality_writer=FailingWriter())
    with caplog.at_level("WARNING"):
        again = await _admit_and_render(h, seed, run="run-2")
    assert again.skipped
    assert "quality usage not declared" in caplog.text
    assert "db down" not in caplog.text
