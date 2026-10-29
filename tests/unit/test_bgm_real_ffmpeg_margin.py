"""固定版 ffmpeg で発話中の声と BGM の差を実測する（ADR-0083 §5、INV-135）。

バイナリ未導入なら skip。合成の声（サイン波）と合成した BGM（``tests/support/bgm_synth``）で、
ducking が発話区間の BGM を実際に下げること、cue が大きいほど差が小さくなることを確かめる。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from contracts.music import DuckingPolicy
from contracts.music_render import BgmMixCue, BgmMixPlan
from domain.music.intelligibility import evaluate_speech_intelligibility
from domain.render.ports import BgmRenderInput
from infrastructure.render.bgm_intelligibility import FfmpegSpeechMarginProbe
from tests.support.bgm_synth import synthesize
from tests.support.render_plans import make_plan

FFMPEG = Path(
    os.environ.get("RENDER_FFMPEG_PATH", str(Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"))
)
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason="static ffmpeg not installed")


def _plan():
    # 6 秒の 1 シーン。声は 1.0〜4.0 秒（発話区間）。それ以外は声なし
    return make_plan(scenes=((6000, 6000),), voices=((1000, 3000),), subtitles=False)


def _mix(ducking: DuckingPolicy) -> BgmMixPlan:
    cue = BgmMixCue(
        scene_id="s1", track_id="bgm-fixture", source_sha256="a" * 64,
        license_evidence_hash="e" * 64, start_ms=0, end_ms=6000, fade_in_ms=100,
        fade_out_ms=100, level_db=-14, ducking=ducking,
    )  # fmt: skip
    return BgmMixPlan(cues=(cue,))


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("bgm-margin")
    voice = d / "voice.wav"
    subprocess.run(  # noqa: S603
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=300:sample_rate=22050:duration=3,volume=4", str(voice)],
        check=True, timeout=60,
    )  # fmt: skip
    track = d / "track.wav"
    track.write_bytes(synthesize(8))
    return voice, track


def _levels(inputs, ducking: DuckingPolicy):
    voice, track = inputs
    probe = FfmpegSpeechMarginProbe(FFMPEG)
    bgm = BgmRenderInput(mix=_mix(ducking), track_paths={"bgm-fixture": track})
    return probe.measure(_plan(), bgm, {"s1": voice})


def test_ducking_lowers_the_music_under_speech_and_the_probe_measures_it(inputs) -> None:
    plain = _levels(inputs, "none")[0]
    ducked = _levels(inputs, "duck_under_voice")[0]
    assert plain.voice_lufs_centi == ducked.voice_lufs_centi  # 声は BGM に影響されない
    assert plain.voice_lufs_centi is not None and ducked.voice_lufs_centi is not None
    assert plain.music_lufs_centi is not None and ducked.music_lufs_centi is not None
    assert ducked.music_lufs_centi < plain.music_lufs_centi - 150  # 1.5 LU 以上下がる
    verdict = evaluate_speech_intelligibility([ducked])
    assert verdict.scenes[0].margin_centi_lu == ducked.voice_lufs_centi - ducked.music_lufs_centi
    margin = verdict.scenes[0].margin_centi_lu
    assert margin is not None and margin > 0


def test_a_louder_cue_gives_a_smaller_margin(inputs) -> None:
    voice, track = inputs
    probe = FfmpegSpeechMarginProbe(FFMPEG)

    def margin(level_db: int) -> int:
        cue = _mix("duck_under_voice").cues[0].model_copy(update={"level_db": level_db})
        bgm = BgmRenderInput(mix=BgmMixPlan(cues=(cue,)), track_paths={"bgm-fixture": track})
        lv = probe.measure(_plan(), bgm, {"s1": voice})[0]
        assert lv.voice_lufs_centi is not None and lv.music_lufs_centi is not None
        return lv.voice_lufs_centi - lv.music_lufs_centi

    assert margin(-12) < margin(-24)
