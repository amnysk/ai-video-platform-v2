"""音声の実尺と台本シーンの区間（描画が使える窓）の適合（ADR-0028）。純粋関数の検査。"""

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
    VOICE_FIT_MAX_RESYNTHESES,
    VoiceTake,
    check_voices_fit_spans,
    next_speed_permille,
)


def _model(fixed_ms: float, variable_ms: float, jitter: tuple[float, ...] = (0.0,)):
    """実測に基づく尺のモデル: 尺 = 固定の間 + 可変部 / 倍率、合成ごとにゆらぎ（±）が乗る。"""
    calls = {"n": 0}

    def synthesize(speed_permille: int) -> int:
        noise = jitter[calls["n"] % len(jitter)]
        calls["n"] += 1
        return round((fixed_ms + variable_ms * 1000 / speed_permille) * (1 + noise))

    return synthesize


def _fit(span_ms: int, synthesize) -> list[VoiceTake]:
    """活動と同じ手順: 等速で合成 → 次の話速が返る間は合成し直す。溢れたままなら例外。"""
    takes = [VoiceTake(NEUTRAL_SPEED_PERMILLE, synthesize(NEUTRAL_SPEED_PERMILLE))]
    while (nxt := next_speed_permille(span_ms=span_ms, takes=takes)) is not None:
        takes.append(VoiceTake(nxt, synthesize(nxt)))
    return takes


def test_no_adjustment_when_voice_already_fits() -> None:
    assert next_speed_permille(span_ms=7000, takes=[VoiceTake(1000, 7000)]) is None
    assert next_speed_permille(span_ms=7000, takes=[VoiceTake(1000, 6000)]) is None


def test_first_estimate_targets_a_little_inside_the_span() -> None:
    """ぴったりを狙うと丸め・句読点の間で再び溢れる。窓の内側に余裕を残して狙う。"""
    speed = next_speed_permille(span_ms=7000, takes=[VoiceTake(1000, 8000)])
    assert speed is not None
    assert speed > 1143  # 8000/7000 だけでは足りない（余裕を含めて、それより大きい）
    assert 8000 * 1000 / speed < 7000


def test_estimate_beyond_the_cap_goes_straight_to_the_cap_not_to_failure() -> None:
    """線形の見積もりが上限を超えても、実測で確かめる前に諦めない。

    尺の一部は話速で縮まない固定の間なので、線形の見積もりは楽観的に外れうる（逆に、
    上限の速さで収まる可能性は残る）。失敗は「上限の速さで実際に合成しても収まらなかった」ときだけ。
    """
    speed = next_speed_permille(span_ms=7000, takes=[VoiceTake(1000, 10147)])
    assert speed == MAX_VOICE_SPEEDUP_PERMILLE


def test_two_measurements_give_a_secant_estimate_for_a_fixed_plus_variable_model() -> None:
    """9/19 s2 の実測型: 20% が固定の間。線形の見積もりでは足りず、2点から固定部を推定する。"""
    natural, span = 10658, 9000
    model = _model(0.2 * natural, 0.8 * natural)
    first = next_speed_permille(span_ms=span, takes=[VoiceTake(1000, natural)])
    assert first is not None
    second = VoiceTake(first, model(first))
    assert second.duration_ms > span  # 線形の見積もり（固定の間を無視）では収まらない
    third = next_speed_permille(span_ms=span, takes=[VoiceTake(1000, natural), second])
    assert third is not None and third > first
    assert third == MAX_VOICE_SPEEDUP_PERMILLE  # 収まる最も遅い速さがここでは上限


def test_prefers_the_slowest_speed_that_fits() -> None:
    """上限へ飛ばない。収まる速さが上限より遅いなら、そこで止まる。"""
    takes = _fit(9000, _model(0.2 * 9800, 0.8 * 9800))  # 9,800 ms を 9,000 ms へ
    assert takes[-1].duration_ms <= 9000
    assert takes[-1].speed_permille < MAX_VOICE_SPEEDUP_PERMILLE
    assert len(takes) == 2


def test_real_data_case_fits_at_the_cap_where_a_linear_search_gave_up() -> None:
    """9/19 の s2（区間 9,000 ms、等速 10,658 ms）は上限 1250‰ で 8,975 ms に収まった。

    線形の見積もりを 2 回だけ繰り返す方式は 9,067 ms で諦めた。上限で実測すれば収まる。
    """
    takes = _fit(9000, _model(0.2 * 10658, 0.8 * 10658, jitter=(0.0, 0.03, 0.0, 0.0)))
    assert takes[-1].duration_ms <= 9000
    assert takes[-1].speed_permille == MAX_VOICE_SPEEDUP_PERMILLE


def test_the_last_permitted_resynthesis_is_always_at_the_cap() -> None:
    """途中の見積もりが外れても、失敗を宣言する前に必ず上限で実測する。"""
    # 等速 → 1回目の見積もり → 2点の外挿は上限より遅い、しかしゆらぎで溢れる、の並び
    takes = [VoiceTake(1000, 9800), VoiceTake(1120, 9100)]
    # 上限までの残りの回数が 1 回になったので、外挿が上限より遅くても上限を返す
    assert (
        next_speed_permille(span_ms=9000, takes=takes, max_resyntheses=len(takes))
        == MAX_VOICE_SPEEDUP_PERMILLE
    )


def test_failure_means_the_cap_speed_measured_too_long() -> None:
    """外挿で諦めない。上限で実測した尺が区間を超えたときだけ失敗し、数値を含める。"""
    takes = [VoiceTake(1000, 14000), VoiceTake(MAX_VOICE_SPEEDUP_PERMILLE, 11500)]
    with pytest.raises(VoiceExceedsSceneSpanError) as info:
        next_speed_permille(span_ms=9000, takes=takes)
    message = str(info.value)
    assert "11500" in message and "9000" in message and str(MAX_VOICE_SPEEDUP_PERMILLE) in message


def test_non_monotonic_measurements_fall_back_to_the_cap() -> None:
    """ゆらぎ・縮まない生成器で 2 点が右下がりにならない。壊れた外挿をせず上限で確かめる。"""
    takes = [VoiceTake(1000, 9000), VoiceTake(1100, 9200)]
    assert next_speed_permille(span_ms=8000, takes=takes) == MAX_VOICE_SPEEDUP_PERMILLE


def test_cap_is_a_parameter_so_profiles_can_differ() -> None:
    speed = next_speed_permille(span_ms=7000, takes=[VoiceTake(1000, 10147)], max_permille=2000)
    assert speed is not None and MAX_VOICE_SPEEDUP_PERMILLE < speed <= 2000


@pytest.mark.parametrize("jitter_pattern", [(0.0,), (0.04, -0.04), (-0.04, 0.04), (0.04,)])
@pytest.mark.parametrize("fixed_fraction", [0.0, 0.1, 0.2, 0.3, 0.4])
@pytest.mark.parametrize("ratio", [1.0, 1.02, 1.1, 1.2, 1.3, 1.5, 2.0])
def test_search_invariants_over_fixed_pause_ratio_and_jitter(
    ratio: float, fixed_fraction: float, jitter_pattern: tuple[float, ...]
) -> None:
    """尺 = 固定 + 可変/倍率、ゆらぎ ±4% のどの組合せでも成り立つこと。

    - 合成は最大 1 + VOICE_FIT_MAX_RESYNTHESES 回、話速は単調増加で上限以下
    - 成功なら最後の合成が区間に収まる。失敗なら、上限で実測して溢れたとき
    - 上限の速さでゆらぎを見込んでも収まる（モデル値 <= 区間 / 1.04）なら、必ず成功する
    """
    span = 8000
    natural = span * ratio
    fixed, variable = fixed_fraction * natural, (1 - fixed_fraction) * natural
    synthesize = _model(fixed, variable, jitter_pattern)
    seen: list[VoiceTake] = []

    def recording(speed: int) -> int:
        duration = synthesize(speed)
        seen.append(VoiceTake(speed, duration))
        return duration

    try:
        takes = _fit(span, recording)
    except VoiceExceedsSceneSpanError:
        assert seen[-1].speed_permille == MAX_VOICE_SPEEDUP_PERMILLE
        assert seen[-1].duration_ms > span
        model_at_cap = fixed + variable * 1000 / MAX_VOICE_SPEEDUP_PERMILLE
        assert model_at_cap > span / 1.04, "上限で収まるはずの入力を、実測せずに諦めた"
    else:
        assert takes[-1].duration_ms <= span
    assert 1 <= len(seen) <= 1 + VOICE_FIT_MAX_RESYNTHESES
    speeds = [t.speed_permille for t in seen]
    assert speeds == sorted(set(speeds)) and speeds[-1] <= MAX_VOICE_SPEEDUP_PERMILLE


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
