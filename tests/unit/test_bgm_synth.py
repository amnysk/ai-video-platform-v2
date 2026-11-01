"""検証用に合成する BGM（ADR-0083 §1）: 第三者の音源を使わないこと・決定的・ループが繋がること。"""

from __future__ import annotations

import io
import wave

import pytest

from tests.support.bgm_synth import SAMPLE_RATE_HZ, synthesize


def _frames(data: bytes) -> tuple[int, list[int]]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 2 and wav.getsampwidth() == 2
        raw = wav.readframes(wav.getnframes())
        rate = wav.getframerate()
    samples = [int.from_bytes(raw[i : i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
    return rate, samples


def test_the_same_arguments_give_the_same_bytes_and_the_expected_length() -> None:
    a, b = synthesize(8), synthesize(8)
    assert a == b
    rate, samples = _frames(a)
    assert rate == SAMPLE_RATE_HZ and len(samples) == 8 * rate * 2


def test_it_is_not_silent_and_does_not_clip() -> None:
    _, samples = _frames(synthesize(8))
    peak = max(abs(s) for s in samples)
    assert 0.5 * 32767 < peak <= 0.95 * 32767


def test_the_loop_seam_is_as_smooth_as_the_music_itself() -> None:
    """終端 → 先頭の跳びが、曲中の隣接サンプルの跳びを大きく超えない（クリックが出ない）。"""
    _, samples = _frames(synthesize(8))
    left = samples[0::2]
    seam = abs(left[0] - left[-1])
    typical = sorted(abs(left[i + 1] - left[i]) for i in range(0, len(left) - 1, 7))
    assert seam <= typical[-1] * 1.5  # 曲中の最大級の隣接差を超えない


def test_length_must_be_a_multiple_of_four_seconds() -> None:
    with pytest.raises(ValueError):
        synthesize(10)
