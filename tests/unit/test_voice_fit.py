"""音声の実尺と台本シーンの区間（描画が使える窓）の適合（ADR-0027）。純粋関数の検査。"""

from __future__ import annotations

import pytest

from contracts.states import FailureClass
from domain.errors import (
    FAILURE_CLASS_BY_TYPE_NAME,
    VoiceExceedsSceneSpanError,
    VoiceTimelineOverflowError,
    classify_failure,
)
from domain.production.voice_fit import (
    MAX_VOICE_SPEEDUP_PERMILLE,
    NEUTRAL_SPEED_PERMILLE,
    check_voices_fit_spans,
    next_speedup_permille,
)


def test_no_adjustment_when_voice_already_fits() -> None:
    assert next_speedup_permille(measured_ms=7000, span_ms=7000, current_permille=1000) is None
    assert next_speedup_permille(measured_ms=6000, span_ms=7000, current_permille=1000) is None


def test_speedup_targets_a_little_inside_the_span() -> None:
    """ぴったりを狙うと丸め・句読点の間で再び溢れる。窓の内側に余裕を残して狙う。"""
    permille = next_speedup_permille(measured_ms=8000, span_ms=7000, current_permille=1000)
    assert permille is not None
    # 8000/7000 = 1.143 だけでは足りない（余裕を含めて、それより大きい）
    assert permille > 1143
    assert 8000 * NEUTRAL_SPEED_PERMILLE / permille < 7000


def test_speedup_is_relative_to_the_speed_already_applied() -> None:
    first = next_speedup_permille(measured_ms=8000, span_ms=7000, current_permille=1000)
    assert first is not None
    # 1回目の結果がまだ 7,100ms なら、その速さを土台にさらに上げる
    second = next_speedup_permille(measured_ms=7100, span_ms=7000, current_permille=first)
    assert second is not None and second > first


def test_speedup_beyond_the_bound_is_refused_not_clamped() -> None:
    """上限を超えて聞き取れない速さにしない。黙って丸めず、失敗として返す。"""
    with pytest.raises(VoiceExceedsSceneSpanError) as info:
        next_speedup_permille(measured_ms=10147, span_ms=7000, current_permille=1000)
    assert "10147" in str(info.value) and "7000" in str(info.value)
    assert MAX_VOICE_SPEEDUP_PERMILLE < 10147 * 1000 // 7000


def test_bound_is_a_parameter_so_profiles_can_differ() -> None:
    assert (
        next_speedup_permille(
            measured_ms=10147, span_ms=7000, current_permille=1000, max_permille=2000
        )
        is not None
    )


def test_check_voices_fit_spans_accepts_exact_fit_and_shorter() -> None:
    check_voices_fit_spans({"s1": 7000, "s2": 9000}, {"s1": 7000, "s2": 100})


def test_check_voices_fit_spans_reports_every_offender() -> None:
    """9/19 の Episode は 5 シーン中 5 つが溢れた。最初の1つだけでなく全部を示す。"""
    with pytest.raises(VoiceExceedsSceneSpanError) as info:
        check_voices_fit_spans(
            {"s1": 7000, "s2": 9000, "s3": 8000}, {"s1": 10147, "s2": 9000, "s3": 10635}
        )
    message = str(info.value)
    assert "s1" in message and "s3" in message and "s2" not in message.replace("span", "")


def test_check_voices_fit_spans_ignores_scenes_without_a_voice_yet() -> None:
    """欠けは manifest の coverage 検査が受け持つ。ここは実尺のある音声だけを見る。"""
    check_voices_fit_spans({"s1": 7000, "s2": 9000}, {"s1": 7000})


def test_exceeds_span_is_needs_input_and_a_timeline_overflow() -> None:
    """再実行しても同じ結果になる欠陥なので retryable にしない（failure-policy）。"""
    err = VoiceExceedsSceneSpanError("x")
    assert isinstance(err, VoiceTimelineOverflowError)
    assert classify_failure(err) is FailureClass.NEEDS_INPUT
    assert FAILURE_CLASS_BY_TYPE_NAME["VoiceExceedsSceneSpanError"] is FailureClass.NEEDS_INPUT
