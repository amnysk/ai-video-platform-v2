"""BGM 入り完成動画のラウドネス検査（ADR-0077 §5、INV-115）。"""

from __future__ import annotations

import pytest

from contracts.music_render import LoudnessMeasurement
from domain.errors import FinalVideoValidationError, RenderEngineFailedError
from domain.render.loudness import (
    LoudnessParseError,
    evaluate_loudness,
    loudness_check,
    parse_ebur128_summary,
)
from domain.render.qa import raise_for_failed_checks
from infrastructure.render.loudness import FfmpegLoudnessProbe

# 固定版 ffmpeg 7.1.1 の ebur128=peak=true:framelog=quiet の実出力（sine 440Hz を -10dB）
REAL_SUMMARY = """\
[Parsed_ebur128_1 @ 0x76a5a7cf5dc0] Summary:

  Integrated loudness:
    I:         -31.8 LUFS
    Threshold: -41.8 LUFS

  Loudness range:
    LRA:        20.0 LU
    Threshold: -51.8 LUFS
    LRA low:   -51.8 LUFS
    LRA high:  -31.8 LUFS

  True peak:
    Peak:      -28.1 dBFS
[out#0/null @ 0x76a5a7d1c6c0] video:0KiB audio:258KiB
"""
# 無音入力（anullsrc）の実出力
SILENT_SUMMARY = """\
[Parsed_ebur128_0 @ 0x78f16cf9f8c0] Summary:

  Integrated loudness:
    I:         -70.0 LUFS
    Threshold:   0.0 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold:   0.0 LUFS
    LRA low:     0.0 LUFS
    LRA high:    0.0 LUFS

  True peak:
    Peak:       -inf dBFS
"""


def _m(i: int | None, p: int | None, lra: int = 500) -> LoudnessMeasurement:
    return LoudnessMeasurement(integrated_lufs_centi=i, true_peak_dbtp_centi=p, lra_centi=lra)


def test_real_ffmpeg_summary_is_parsed_into_centi_units() -> None:
    m = parse_ebur128_summary(REAL_SUMMARY)
    assert (m.integrated_lufs_centi, m.true_peak_dbtp_centi, m.lra_centi) == (-3180, -2810, 2000)


def test_silent_summary_has_no_peak_and_is_a_silent_failure() -> None:
    m = parse_ebur128_summary(SILENT_SUMMARY)
    assert m.true_peak_dbtp_centi is None and m.integrated_lufs_centi == -7000
    verdict = evaluate_loudness(m)
    assert not verdict.passed and verdict.reason_code == "silent"


@pytest.mark.parametrize("text", ["", "no summary here", REAL_SUMMARY.replace("I:", "X:")])
def test_unreadable_output_is_an_error_never_a_pass(text: str) -> None:
    with pytest.raises(LoudnessParseError):
        parse_ebur128_summary(text)


@pytest.mark.parametrize(
    ("integrated", "peak", "reason"),
    [
        (-1400, -300, "ok"),
        (-2400, -100, "ok"),  # 下限・天井ちょうどは合格
        (-900, -100, "ok"),  # 上限ちょうどは合格
        (-2401, -300, "too_quiet"),
        (-899, -300, "too_loud"),
        (-1400, -99, "true_peak_exceeded"),
        (-899, -99, "true_peak_exceeded"),  # 複数違反は天井超過を先に報告
        (None, None, "silent"),
        (-7000, -2000, "silent"),
    ],
)
def test_verdict_reason_codes_and_boundaries(integrated, peak, reason: str) -> None:
    verdict = evaluate_loudness(_m(integrated, peak))
    assert verdict.reason_code == reason
    assert verdict.passed is (reason == "ok")
    assert verdict.measured.integrated_lufs_centi == integrated


def test_a_failed_verdict_becomes_a_failed_qa_check_that_raises() -> None:
    check = loudness_check(evaluate_loudness(_m(-600, -300)))
    assert check.check == "bgm_loudness" and not check.passed
    assert "too_loud" in check.detail and "-600" in check.detail
    with pytest.raises(FinalVideoValidationError):
        raise_for_failed_checks([check])
    raise_for_failed_checks([loudness_check(evaluate_loudness(_m(-1400, -300)))])


def test_probe_that_cannot_run_raises_instead_of_passing(tmp_path) -> None:
    probe = FfmpegLoudnessProbe(tmp_path / "missing-ffmpeg")
    with pytest.raises(RenderEngineFailedError):
        probe.measure_loudness(str(tmp_path / "x.mp4"))
