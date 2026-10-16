"""合成した音声の実尺を、描画が使える区間へ合わせる規則（ADR-0028）。純粋関数のみ。

音声の長さは合成するまで分からない。台本・storyboard の読み上げ予算（ADR-0026）は推定で、
保証ではない。ここは**合成後の実尺**で「音声 <= 区間」を決定論的に判定し、収まらなければ
話速を上げて合成し直す速さを決める。

尺は話速に反比例しない。実測では尺の約 2 割が話速で縮まない固定の間で、同じ入力でも
合成ごとに ±4% ゆらぐ。そのため線形の外挿を信用せず、実測（``VoiceTake``）で探す:

1. 実測 1 点だけなら線形の見積もり（余裕つき）。上限を超えるなら、諦めず上限で実測する
2. 実測 2 点以上なら、直近の 2 点から ``尺 = 固定 + 可変 / 倍率`` を当てはめて次を決める
   （右下がりにならない・当てはまらないときは上限）
3. 最後に許された再合成は、途中の見積もりに関わらず**必ず上限**にする
4. 失敗は「上限の速さで実測して、なお区間を超えた」ときだけ ``VoiceExceedsSceneSpanError``

収まった最初の実測が採用されるので、収まる最も遅い速さ（試した中で）になる。
合成は最大 ``1 + VOICE_FIT_MAX_RESYNTHESES`` 回。

区間の長さは content / render profile に依らず、storyboard の並びだけで決まる
（``domain.storyboard.coverage.script_scene_spans``）。この module に尺の定数は無い。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from domain.errors import VoiceExceedsSceneSpanError

#: 等速
NEUTRAL_SPEED_PERMILLE = 1000
#: 話速を上げてよい上限（等速の 1.25 倍）。これ以上は聞き取りにくくなるので、調整せず失敗にする。
#: 生成器・言語ごとに変えたくなったら引数の ``max_permille`` で渡す。
MAX_VOICE_SPEEDUP_PERMILLE = 1250
#: 合成し直すときに区間の内側へ残す余裕（permille）。ぴったりを狙うと丸め・ゆらぎで再び溢れる。
VOICE_FIT_HEADROOM_PERMILLE = 30
#: 話速を上げて合成し直してよい回数の上限（等速の 1 回と合わせて最大 4 回）。
#: voice_concurrency 1 で直列に走るので、壁時計に効く。最後の 1 回は必ず上限の速さ。
VOICE_FIT_MAX_RESYNTHESES = 3
#: 2 回目以降の見積もりが前より遅く（同じに）ならないよう、最低限上げる幅（permille）
_MIN_STEP_PERMILLE = 20
#: Artifact の ``generation_profile_id`` の長さの上限（contracts.artifacts.GeneratorMetadata）
_PROFILE_ID_MAX_LEN = 128


@dataclass(frozen=True, slots=True)
class VoiceTake:
    """ある話速（permille、等速 = 1000）で合成した実測。"""

    speed_permille: int
    duration_ms: int


def _estimate_from_takes(takes: Sequence[VoiceTake], target_ms: int) -> int | None:
    """``尺 = 固定 + 可変 / 倍率`` を直近の 2 点に当てはめ、目標尺に届く話速を返す。

    1 点しか無ければ固定を 0 とみなす線形の見積もり。当てはまらない（2 点が右下がりでない、
    目標が固定部以下）ときは ``None``（呼び出し側が上限で確かめる）。
    """
    last = takes[-1]
    if len(takes) == 1:
        return -(-last.speed_permille * last.duration_ms // target_ms)  # 切り上げ
    prev = takes[-2]
    # x = 1000 / 話速（1 = 等速）。尺 = fixed + variable * x
    x_last, x_prev = 1000 / last.speed_permille, 1000 / prev.speed_permille
    if x_last >= x_prev or last.duration_ms >= prev.duration_ms:
        return None
    variable = (prev.duration_ms - last.duration_ms) / (x_prev - x_last)
    fixed = max(0.0, last.duration_ms - variable * x_last)
    if target_ms <= fixed:
        return None
    return -int(-1000 * variable // (target_ms - fixed))  # 切り上げ


def next_speed_permille(
    *,
    span_ms: int,
    takes: Sequence[VoiceTake],
    max_permille: int = MAX_VOICE_SPEEDUP_PERMILLE,
    max_resyntheses: int = VOICE_FIT_MAX_RESYNTHESES,
) -> int | None:
    """収まっていれば ``None``。溢れていれば次に合成する話速（permille）。

    ``takes`` は合成した順の実測（最初は等速）。最後の実測が区間に収まれば ``None``（採用）。
    上限の速さで実測してなお超える、または再合成の回数を使い切ったら
    ``VoiceExceedsSceneSpanError``（丸めない・外挿で諦めない）。
    """
    last = takes[-1]
    if last.duration_ms <= span_ms:
        return None
    resyntheses = len(takes) - 1
    if last.speed_permille >= max_permille or resyntheses >= max_resyntheses:
        raise VoiceExceedsSceneSpanError(
            f"voice is {last.duration_ms} ms at speed {last.speed_permille}\u2030 but the scene "
            f"span is {span_ms} ms (cap {max_permille}\u2030, {len(takes)} syntheses: "
            + ", ".join(f"{t.speed_permille}\u2030={t.duration_ms} ms" for t in takes)
            + ")"
        )
    target_ms = span_ms * (1000 - VOICE_FIT_HEADROOM_PERMILLE) // 1000
    if target_ms <= 0:
        raise VoiceExceedsSceneSpanError(
            f"voice is {last.duration_ms} ms but the scene span is {span_ms} ms (no room to fit)"
        )
    if resyntheses == max_resyntheses - 1:
        return max_permille  # 最後の 1 回は必ず上限で実測する
    estimate = _estimate_from_takes(takes, target_ms)
    if estimate is None:
        return max_permille
    return min(max_permille, max(estimate, last.speed_permille + _MIN_STEP_PERMILLE))


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
    "VoiceTake",
    "check_voices_fit_spans",
    "fitted_profile_id",
    "next_speed_permille",
]
