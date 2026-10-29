"""BGM だけを変えても fal を 1 回も呼ばず、production の成果物・入力 hash は動かない（INV-137）。

fal クライアントの全メソッドを「呼ばれたら失敗」にし、呼ばれた回数を数える。cue sheet は本物の
配置関数（``plan_cue_sheet``）が作る。トラック・配置・役割を変えるたびに render の input_hash
だけが変わる。
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from contracts.states import ArtifactType, JobType
from domain.music.placement import SceneRole, plan_cue_sheet
from infrastructure.db.models import ArtifactMetadataRow, ProviderReservationRow
from infrastructure.db.repositories import JobRepository
from tests.support.bgm_catalog import make_track
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import Harness  # noqa: F401
from tests.unit.test_render_activities_bgm import (
    FakeBgmSource,
    _admit_and_render,
    _harness,
    _input_hashes,
)

LOUD = {"loudness_lufs": -18.0}


def _spy_on_fal(monkeypatch) -> Counter[str]:
    from infrastructure.providers import fal_queue, fal_storage

    calls: Counter[str] = Counter()

    def spy(name: str):
        async def _fail(*args: object, **kwargs: object):
            calls[name] += 1
            raise AssertionError(f"fal must not be called from render: {name}")

        return _fail

    for cls in (fal_queue.FalQueueClient, fal_storage.FalStorageClient):
        for attr in ("submit", "status", "result", "download", "upload", "upload_file"):
            if hasattr(cls, attr):
                monkeypatch.setattr(cls, attr, spy(f"{cls.__name__}.{attr}"))
    return calls


async def _production_state(session_factory, episode_id: str):
    """production 側の全成果物（id・版・sha256・入力 hash）・予約台帳の件数・job の種類。"""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    ArtifactMetadataRow.id,
                    ArtifactMetadataRow.artifact_type,
                    ArtifactMetadataRow.version,
                    ArtifactMetadataRow.sha256,
                    ArtifactMetadataRow.input_hash,
                    ArtifactMetadataRow.content_fingerprint,
                ).where(ArtifactMetadataRow.artifact_type != ArtifactType.FINAL_VIDEO.value)
            )
        ).all()
        reservations = await session.scalar(
            select(func.count()).select_from(ProviderReservationRow)
        )
        jobs = {j.type.value for j in await JobRepository(session).list_for_episode(episode_id)}
    return sorted(map(tuple, rows)), reservations, jobs


async def test_changing_only_the_track_or_the_placement_reruns_render_and_nothing_else(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    fal_calls = _spy_on_fal(monkeypatch)
    seed = await seed_render_inputs(session_factory, artifact_store)
    plain = _harness(session_factory, artifact_store, tmp_path / "plain")
    await _admit_and_render(plain, seed)
    plan = plain.engine.requests[-1].plan
    before = await _production_state(session_factory, seed.episode_id)

    track_a = make_track("bgm-a", "a", **LOUD)
    track_b = make_track("bgm-b", "b", **LOUD)
    source = FakeBgmSource(tmp_path, (track_a, track_b), None)
    h = _harness(session_factory, artifact_store, tmp_path / "bgm", source)

    variants = [
        plan_cue_sheet(plan, track_a),  # BGM を付ける
        plan_cue_sheet(plan, track_a, roles={"s2": SceneRole.CLIMAX}),  # 配置だけ変える
        plan_cue_sheet(plan, track_b),  # 曲だけ変える
    ]
    rendered = []
    for i, result in enumerate(variants, start=2):
        assert result.cue_sheet is not None
        source.cue_sheet = result.cue_sheet
        h.engine.payload = f"video-with-bgm-{i}".encode()  # 実エンジンは音声が変われば出力も変わる
        rendered.append(await _admit_and_render(h, seed, run=f"run-{i}"))
    again = await _admit_and_render(h, seed, run="run-9")  # 変更なし = 描画しない

    assert not any(r.skipped for r in rendered) and again.skipped
    assert len(h.engine.requests) == len(variants)  # 描画したのは render だけ（変更の数だけ）
    assert sum(fal_calls.values()) == 0  # fal の呼び出し 0 回
    after = await _production_state(session_factory, seed.episode_id)
    # production の成果物・入力 hash・予約台帳（0 件）・job の種類が不変
    # （job は種類だけ見る。render の job は回数分増える）
    assert after == before
    assert after[1] == 0 and after[2] == {JobType.RENDER_FINAL_VIDEO.value}
    hashes = await _input_hashes(session_factory, seed.episode_id)
    assert len(hashes) == 4 and len(set(hashes)) == 4  # BGM 無し + 3 通りが全て別の render 入力
