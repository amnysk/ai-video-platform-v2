"""VoiceActivities が、合成後の実尺を台本シーンの区間へ合わせる（ADR-0028）。

台本シーン s1 の区間は 8,000 ms（tests.support.voice.STORYBOARD_LAYOUT）。Temporal を介さず
直接呼ぶ。SQLite + InMemoryArtifactStore + 尺を文字数で決める Fake（INV-18）。
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from contracts.artifacts import parse_scene_voice_artifact
from contracts.states import ArtifactType, FailureClass, JobStatus, JobType
from domain.production.voice_fit import MAX_VOICE_SPEEDUP_PERMILLE, VOICE_FIT_MAX_RESYNTHESES
from infrastructure.db.repositories import ArtifactMetadataRepository, JobRepository
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeVoiceGenerator, make_wav
from tests.support.voice import BUCKET, create_episode, record_inputs, voice_request
from workers.production_voice.activities import VoiceActivities

S1_SPAN_MS = 8000
LONG = "あ" * 100  # FakeVoiceGenerator は 1 文字 ms_per_char


class SpeedAdjustableFake(FakeVoiceGenerator):
    """話速を指定できる生成器。尺は話速に反比例する。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.speeds: list[int] = []

    async def synthesize_at_speed(self, text, language, dest, *, speed_permille: int) -> None:
        self.calls += 1
        self.speeds.append(speed_permille)
        base = min(60_000, max(500, len(text) * self.ms_per_char))
        await dest.write(make_wav(base * 1000 // speed_permille))


class MeasuredModelFake(FakeVoiceGenerator):
    """実測に基づく尺のモデル: 尺 = 固定の間 + 可変部 / 倍率、合成ごとにゆらぎ（±）が乗る。

    9/19 の実 Piper では、尺の約 2 割が話速で縮まない固定の間で、同じ入力でも ±4% ゆらいだ。
    """

    def __init__(self, *, natural_ms: int, fixed_fraction: float = 0.2, jitter=(0.0,)) -> None:
        super().__init__()
        self.fixed = natural_ms * fixed_fraction
        self.variable = natural_ms * (1 - fixed_fraction)
        self.jitter = jitter
        self.speeds: list[int] = []

    def _duration(self, speed_permille: int) -> int:
        noise = self.jitter[self.calls % len(self.jitter)]
        self.calls += 1
        return round((self.fixed + self.variable * 1000 / speed_permille) * (1 + noise))

    async def synthesize(self, text, language, dest) -> None:
        await dest.write(make_wav(self._duration(1000)))

    async def synthesize_at_speed(self, text, language, dest, *, speed_permille: int) -> None:
        self.speeds.append(speed_permille)
        await dest.write(make_wav(self._duration(speed_permille)))


class StubbornFake(SpeedAdjustableFake):
    """話速を上げても尺が縮まない（引数を無視する壊れた生成器）。"""

    async def synthesize_at_speed(self, text, language, dest, *, speed_permille: int) -> None:
        self.speeds.append(speed_permille)
        await self.synthesize(text, language, dest)


@pytest.fixture
def store() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


@pytest.fixture
def workdir(tmp_path) -> WorkDirectory:
    return WorkDirectory(tmp_path / "work")


def make(session_factory, store, workdir, generator) -> VoiceActivities:
    return VoiceActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        workdir=workdir,
        bucket=BUCKET,
    )


async def _run(session_factory, store, workdir, generator, narration, scene="s1"):
    episode = await create_episode(session_factory)
    script, storyboard = await record_inputs(
        session_factory, store, episode, narrations={scene: narration}
    )
    activities = make(session_factory, store, workdir, generator)
    return episode, activities, voice_request(episode, script, storyboard, scene)


async def test_voice_longer_than_span_is_resynthesized_faster_to_fit(
    session_factory, store, workdir
) -> None:
    """Test A の核: 9/19 型（実尺 > 区間）でも、Render 前に区間へ収まった音声になる。"""
    generator = SpeedAdjustableFake(ms_per_char=90)  # 100 字 → 9,000 ms > 8,000 ms
    _, activities, request = await _run(session_factory, store, workdir, generator, LONG)

    result = await activities.generate_voice(request)

    artifact = parse_scene_voice_artifact(await store.get_json(result.object_key))
    assert artifact.duration_ms <= S1_SPAN_MS
    assert generator.calls == 2 and len(generator.speeds) == 1
    assert 1000 < generator.speeds[0] <= MAX_VOICE_SPEEDUP_PERMILLE
    # 実際に使った話速が来歴に残る（同じ生成器の等速の音声と区別できる）
    assert artifact.generator.generation_profile_id.startswith(generator.generation_profile_id)
    assert artifact.generator.generation_profile_id != generator.generation_profile_id
    assert len(artifact.generator.generation_profile_id) <= 128


async def test_voice_that_fits_is_synthesized_once_at_neutral_speed(
    session_factory, store, workdir
) -> None:
    """収まる音声は触らない（ja の既存台本・短い英語を新しい経路へ巻き込まない）。"""
    generator = SpeedAdjustableFake(ms_per_char=60)
    _, activities, request = await _run(
        session_factory, store, workdir, generator, "縄文土器には焦げ跡が残る。"
    )

    result = await activities.generate_voice(request)

    assert generator.calls == 1 and generator.speeds == []
    artifact = parse_scene_voice_artifact(await store.get_json(result.object_key))
    assert artifact.generator.generation_profile_id == generator.generation_profile_id


async def test_voice_beyond_the_speed_bound_fails_as_needs_input_before_render(
    session_factory, store, workdir
) -> None:
    """上限を超える速さにしない。黙って丸めず、有料の制作の前に needs_input で止める。"""
    generator = SpeedAdjustableFake(ms_per_char=120)  # 12,000 ms は 1.25 倍でも収まらない
    episode, activities, request = await _run(session_factory, store, workdir, generator, LONG)

    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(request)

    assert info.value.type == "VoiceExceedsSceneSpanError"
    assert info.value.non_retryable is True
    [job] = [
        j
        for j in await _list_jobs(session_factory, episode)
        if j.type is JobType.PRODUCE_SCENE_VOICE
    ]
    assert job.failure_class is FailureClass.NEEDS_INPUT
    assert job.status is not JobStatus.RETRYABLE_FAILED
    async with session_factory() as session:  # 溢れる音声を現行の Artifact として残さない
        assert (
            await ArtifactMetadataRepository(session).list_current_by_type(
                episode, ArtifactType.SCENE_VOICE
            )
            == []
        )


async def test_generator_without_speed_control_fails_instead_of_overflowing_later(
    session_factory, store, workdir
) -> None:
    generator = FakeVoiceGenerator(ms_per_char=90)  # synthesize_at_speed を持たない
    _, activities, request = await _run(session_factory, store, workdir, generator, LONG)

    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(request)

    assert info.value.type == "VoiceExceedsSceneSpanError"
    assert generator.calls == 1  # 直せないので合成し直さない


async def test_resynthesis_is_bounded_when_speed_does_not_shorten_the_voice(
    session_factory, store, workdir
) -> None:
    """尺が縮まない生成器でも無限に合成し直さない（上限付きの再合成）。"""
    generator = StubbornFake(ms_per_char=90)
    _, activities, request = await _run(session_factory, store, workdir, generator, LONG)

    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(request)

    assert info.value.type == "VoiceExceedsSceneSpanError"
    assert len(generator.speeds) <= VOICE_FIT_MAX_RESYNTHESES
    assert generator.calls == 1 + len(generator.speeds)


async def test_last_scene_is_held_to_the_storyboard_end_too(
    session_factory, store, workdir
) -> None:
    """最後の台本シーンの区間は storyboard の終端まで（描画の freeze 延長は余裕に数えない）。"""
    generator = SpeedAdjustableFake(ms_per_char=90)  # s3 の区間は 8,000 ms
    _, activities, request = await _run(
        session_factory, store, workdir, generator, LONG, scene="s3"
    )

    result = await activities.generate_voice(request)

    artifact = parse_scene_voice_artifact(await store.get_json(result.object_key))
    assert artifact.duration_ms <= 8000 and len(generator.speeds) == 1


S2_SPAN_MS = 9000  # STORYBOARD_LAYOUT: s2 は 8,000〜17,000 ms


async def test_real_data_case_fits_at_the_cap_speed_where_a_linear_search_gave_up(
    session_factory, store, workdir
) -> None:
    """9/19 s2 の実測型（区間 9,000 ms、等速 10,658 ms、固定の間 2 割、ゆらぎ）。

    線形の見積もりを 2 回だけ繰り返す方式は 9,067 ms で VoiceExceedsSceneSpanError にしたが、
    上限（1250‰）の速さでは 8,975 ms で収まった。失敗を宣言する前に上限で実測する。
    """
    generator = MeasuredModelFake(natural_ms=10658, jitter=(0.0, 0.03, 0.0, 0.0))
    _, activities, request = await _run(session_factory, store, workdir, generator, "x", scene="s2")

    result = await activities.generate_voice(request)

    artifact = parse_scene_voice_artifact(await store.get_json(result.object_key))
    assert artifact.duration_ms <= S2_SPAN_MS
    assert generator.speeds[-1] == MAX_VOICE_SPEEDUP_PERMILLE
    assert generator.calls <= 1 + VOICE_FIT_MAX_RESYNTHESES
    assert artifact.generator.generation_profile_id.endswith(f"+fit{MAX_VOICE_SPEEDUP_PERMILLE}")


async def test_a_mild_overrun_is_fitted_at_a_speed_below_the_cap(
    session_factory, store, workdir
) -> None:
    """収まる最も遅い速さを採る。上限へ飛ばして聞こえ方を必要以上に変えない。"""
    generator = MeasuredModelFake(natural_ms=9600)  # 区間 9,000 ms へ
    _, activities, request = await _run(session_factory, store, workdir, generator, "x", scene="s2")

    result = await activities.generate_voice(request)

    artifact = parse_scene_voice_artifact(await store.get_json(result.object_key))
    assert artifact.duration_ms <= S2_SPAN_MS
    assert 1000 < generator.speeds[-1] < MAX_VOICE_SPEEDUP_PERMILLE
    assert generator.calls == 2


async def test_failure_is_declared_only_after_the_cap_speed_was_measured(
    session_factory, store, workdir
) -> None:
    """失敗 = 上限の速さで実際に合成してなお区間を超えた。外挿だけで諦めない。合成は有限回。"""
    generator = MeasuredModelFake(natural_ms=14000, jitter=(0.04, -0.04))
    episode, activities, request = await _run(
        session_factory, store, workdir, generator, "x", scene="s2"
    )

    with pytest.raises(ApplicationError) as info:
        await activities.generate_voice(request)

    assert info.value.type == "VoiceExceedsSceneSpanError" and info.value.non_retryable is True
    assert generator.speeds[-1] == MAX_VOICE_SPEEDUP_PERMILLE
    assert generator.calls <= 1 + VOICE_FIT_MAX_RESYNTHESES
    assert "s2" in str(info.value) and str(MAX_VOICE_SPEEDUP_PERMILLE) in str(info.value)
    [job] = [
        j
        for j in await _list_jobs(session_factory, episode)
        if j.type is JobType.PRODUCE_SCENE_VOICE
    ]
    assert job.failure_class is FailureClass.NEEDS_INPUT


async def _list_jobs(session_factory, episode_id):
    async with session_factory() as session:
        return await JobRepository(session).list_for_episode(episode_id)
