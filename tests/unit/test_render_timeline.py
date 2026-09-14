"""描画の時間軸（ADR-0019 §3）。"""

from __future__ import annotations

import pytest

from contracts.render import TimelinePolicy
from domain.errors import (
    DurationReconciliationError,
    RenderInputIntegrityError,
    RenderInputMissingError,
    VoiceTimelineOverflowError,
)
from domain.render.timeline import (
    SceneVideoSource,
    VoiceSource,
    build_scene_timeline,
    build_timeline,
    reconcile_scene_duration,
)
from tests.support.production import sample_script, sample_storyboard
from tests.support.render_fixtures import STORYBOARD_DURATIONS, VOICE_DURATIONS, VOICE_SCENES

SHA = "c" * 64
AID = "00000000-0000-4000-8000-000000000001"


def _videos(**durations: int) -> dict[str, SceneVideoSource]:
    return {
        sid: SceneVideoSource(sid, AID, SHA, durations.get(sid, requested))
        for sid, requested in STORYBOARD_DURATIONS.items()
    }


def _voices(**durations: int) -> dict[str, VoiceSource]:
    return {
        sid: VoiceSource(sid, AID, SHA, durations.get(sid, default), VOICE_SCENES[sid])
        for sid, default in VOICE_DURATIONS.items()
    }


@pytest.mark.parametrize(
    ("requested", "source", "mode", "trim", "freeze"),
    [
        (5000, 5000, "exact", 0, 0),
        (5000, 5400, "trim", 400, 0),
        (5000, 4000, "freeze_tail", 0, 1000),
        (5000, 3000, "freeze_tail", 0, 2000),
    ],
)
def test_reconcile_modes(requested: int, source: int, mode: str, trim: int, freeze: int) -> None:
    r = reconcile_scene_duration(
        scene_id="sb1", requested_ms=requested, source_ms=source, max_freeze_ms=2000
    )
    assert (r.mode, r.trim_ms, r.freeze_ms) == (mode, trim, freeze)


def test_reconcile_beyond_freeze_limit_is_needs_input() -> None:
    with pytest.raises(DurationReconciliationError):
        reconcile_scene_duration(
            scene_id="sb1", requested_ms=5000, source_ms=2999, max_freeze_ms=2000
        )


def test_scene_timeline_is_contiguous_in_storyboard_order() -> None:
    sb = sample_storyboard("ep")
    scenes = build_scene_timeline(sb, _videos(sb2=4500, sb3=4000), TimelinePolicy())
    assert [s.scene_id for s in scenes] == ["sb1", "sb2", "sb3", "sb4"]
    assert [s.timeline_start_ms for s in scenes] == [0, 8000, 12000, 17000]
    assert [s.timeline_duration_ms for s in scenes] == [8000, 4000, 5000, 8000]
    assert scenes[1].reconciliation.mode == "trim"
    assert scenes[2].reconciliation.mode == "freeze_tail"


def test_missing_scene_video_is_missing_input() -> None:
    videos = _videos()
    del videos["sb3"]
    with pytest.raises(RenderInputMissingError):
        build_scene_timeline(sample_storyboard("ep"), videos, TimelinePolicy())


def test_voices_start_at_their_first_storyboard_scene() -> None:
    layout = build_timeline(
        sample_storyboard("ep"), sample_script("ep"), _videos(), _voices(), TimelinePolicy()
    )
    assert [(v.script_scene_id, v.start_ms, v.duration_ms) for v in layout.voices] == [
        ("s1", 0, 7000),
        ("s2", 8000, 8500),
        ("s3", 17000, 7500),
    ]
    assert layout.voices[1].storyboard_scene_ids == ("sb2", "sb3")
    assert layout.total_duration_ms == 25000


def test_voice_may_spill_into_the_next_scene_gap_but_not_overlap() -> None:
    # s1 は 8000ms の窓に 8000ms ぴったりまで入る
    build_timeline(
        sample_storyboard("ep"), sample_script("ep"), _videos(), _voices(s1=8000), TimelinePolicy()
    )
    with pytest.raises(VoiceTimelineOverflowError):
        build_timeline(
            sample_storyboard("ep"),
            sample_script("ep"),
            _videos(),
            _voices(s1=8001),
            TimelinePolicy(),
        )


def test_last_voice_extends_the_last_scene_within_max_freeze() -> None:
    layout = build_timeline(
        sample_storyboard("ep"),
        sample_script("ep"),
        _videos(),
        _voices(s3=9500),
        TimelinePolicy(max_freeze_ms=2000),
    )
    last = layout.scenes[-1]
    assert layout.total_duration_ms == 26500
    assert last.timeline_duration_ms == 9500
    assert (last.reconciliation.mode, last.reconciliation.freeze_ms) == ("freeze_tail", 1500)
    assert last.requested_duration_ms == 8000


def test_last_voice_extension_first_consumes_trim() -> None:
    layout = build_timeline(
        sample_storyboard("ep"),
        sample_script("ep"),
        _videos(sb4=9000),
        _voices(s3=8500),
        TimelinePolicy(),
    )
    last = layout.scenes[-1]
    assert (last.reconciliation.mode, last.reconciliation.trim_ms) == ("trim", 500)
    assert last.timeline_duration_ms == 8500


def test_last_voice_overflow_beyond_max_freeze_is_needs_input() -> None:
    with pytest.raises(VoiceTimelineOverflowError):
        build_timeline(
            sample_storyboard("ep"),
            sample_script("ep"),
            _videos(),
            _voices(s3=10001),
            TimelinePolicy(max_freeze_ms=2000),
        )


def test_total_freeze_on_the_last_scene_is_capped() -> None:
    # 最終シーンが既に 1500ms freeze。音声で +1000ms 延ばすと 2500ms > 2000ms
    with pytest.raises(VoiceTimelineOverflowError):
        build_timeline(
            sample_storyboard("ep"),
            sample_script("ep"),
            _videos(sb4=6500),
            _voices(s3=9000),
            TimelinePolicy(max_freeze_ms=2000),
        )


def test_voice_scene_ids_must_match_the_storyboard() -> None:
    voices = _voices()
    voices["s2"] = VoiceSource("s2", AID, SHA, 8000, ("sb2",))
    with pytest.raises(RenderInputIntegrityError):
        build_timeline(
            sample_storyboard("ep"), sample_script("ep"), _videos(), voices, TimelinePolicy()
        )


def test_missing_and_extra_voices() -> None:
    voices = _voices()
    del voices["s2"]
    with pytest.raises(RenderInputMissingError):
        build_timeline(
            sample_storyboard("ep"), sample_script("ep"), _videos(), voices, TimelinePolicy()
        )
    voices = _voices()
    voices["s9"] = VoiceSource("s9", AID, SHA, 100, ("sb4",))
    with pytest.raises(RenderInputIntegrityError):
        build_timeline(
            sample_storyboard("ep"), sample_script("ep"), _videos(), voices, TimelinePolicy()
        )


def test_last_voice_overflow_beyond_max_freeze_is_allowed_when_trim_covers_it() -> None:
    # 動画 12000ms を 8000ms に trim 中。音声が 3000ms はみ出しても trim を戻せば freeze 不要
    layout = build_timeline(
        sample_storyboard("ep"),
        sample_script("ep"),
        _videos(sb4=12000),
        _voices(s3=11000),
        TimelinePolicy(max_freeze_ms=2000),
    )
    last = layout.scenes[-1]
    assert (last.reconciliation.mode, last.reconciliation.trim_ms) == ("trim", 1000)
    assert layout.total_duration_ms == 28000
