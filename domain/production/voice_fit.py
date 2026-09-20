"""合成した音声の実尺を、描画が使える区間へ合わせる規則（ADR-0027）。純粋関数のみ。

音声の長さは合成するまで分からない。台本・storyboard の読み上げ予算（ADR-0026）は推定で、
保証ではない。ここは**合成後の実尺**で「音声 <= 区間」を決定論的に判定し、収まらなければ
話速を上げて合成し直す量を決める。上限（``MAX_VOICE_SPEEDUP_PERMILLE``）を超える調整は
しない。超えるなら黙って丸めず ``VoiceExceedsSceneSpanError``。

区間の長さは content / render profile に依らず、storyboard の並びだけで決まる
（``domain.storyboard.coverage.script_scene_spans``）。この module に尺の定数は無い。
"""

from __future__ import annotations

from collections.abc import Mapping

from domain.errors import VoiceExceedsSceneSpanError

#: 等速
NEUTRAL_SPEED_PERMILLE = 1000
#: 話速を上げてよい上限（等速の 1.25 倍）。これ以上は聞き取りにくくなるので、調整せず失敗にする。
#: 生成器・言語ごとに変えたくなったら引数の ``max_permille`` で渡す。
MAX_VOICE_SPEEDUP_PERMILLE = 1250
#: 合成し直すときに区間の内側へ残す余裕（permille）。ぴったりを狙うと丸めで再び溢れる。
VOICE_FIT_HEADROOM_PERMILLE = 30
#: 話速を上げて合成し直してよい回数の上限。尺が縮まない生成器でも無限に繰り返さない。
VOICE_FIT_MAX_RESYNTHESES = 2
#: Artifact の ``generation_profile_id`` の長さの上限（contracts.artifacts.GeneratorMetadata）
_PROFILE_ID_MAX_LEN = 128


def next_speedup_permille(
    *,
    measured_ms: int,
    span_ms: int,
    current_permille: int,
    max_permille: int = MAX_VOICE_SPEEDUP_PERMILLE,
) -> int | None:
    """収まっていれば ``None``。溢れていれば、次に合成するときの話速（permille、等速 = 1000）。

    ``measured_ms`` は ``current_permille`` の話速で合成した実尺。尺は話速にほぼ反比例するので、
    区間の内側（余裕つき）に収まる倍率を、今の話速に掛けて返す。上限を超えるなら
    ``VoiceExceedsSceneSpanError``（丸めない）。
    """
    if measured_ms <= span_ms:
        return None
    target_ms = span_ms * (1000 - VOICE_FIT_HEADROOM_PERMILLE) // 1000
    if target_ms <= 0:
        raise VoiceExceedsSceneSpanError(
            f"voice is {measured_ms} ms but the scene span is {span_ms} ms (no room to fit)"
        )
    needed = -(-current_permille * measured_ms // target_ms)  # 切り上げ
    if needed > max_permille:
        raise VoiceExceedsSceneSpanError(
            f"voice is {measured_ms} ms but the scene span is {span_ms} ms; fitting it needs "
            f"speed {needed}‰ (> max {max_permille}‰, current {current_permille}‰)"
        )
    return needed


def fitted_profile_id(base_profile_id: str, speed_permille: int) -> str:
    """話速を上げて合成した音声の ``generation_profile_id``。等速なら基の id のまま。

    実際に使った話速を来歴に残す（等速の音声と別の生成として区別できる）。
    """
    if speed_permille == NEUTRAL_SPEED_PERMILLE:
        return base_profile_id
    suffix = f"+fit{speed_permille}"
    return base_profile_id[: _PROFILE_ID_MAX_LEN - len(suffix)] + suffix


def check_voices_fit_spans(spans: Mapping[str, int], durations: Mapping[str, int]) -> None:
    """実尺のある音声がすべて自分の区間に収まること。違反は全件を1つの例外で返す。

    ``spans`` は台本シーン id → 区間 ms、``durations`` は台本シーン id → 音声の実尺 ms。
    音声が無いシーンは見ない（欠けは manifest の coverage 検査が受け持つ）。
    """
    over = [
        f"{scene_id}: voice {duration} ms > span {spans[scene_id]} ms"
        for scene_id, duration in durations.items()
        if scene_id in spans and duration > spans[scene_id]
    ]
    if over:
        raise VoiceExceedsSceneSpanError("; ".join(over))


__all__ = [
    "MAX_VOICE_SPEEDUP_PERMILLE",
    "NEUTRAL_SPEED_PERMILLE",
    "VOICE_FIT_HEADROOM_PERMILLE",
    "VOICE_FIT_MAX_RESYNTHESES",
    "check_voices_fit_spans",
    "fitted_profile_id",
    "next_speedup_permille",
]
