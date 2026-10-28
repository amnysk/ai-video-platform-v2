"""BGM 入り完成動画のラウドネス検査（ADR-0077 §5、INV-115）。純粋関数。

描画エンジンの ``ebur128`` の要約（stderr）を読み、数値（1/100 単位の int）と理由コードを持つ
構造化結果にする。読めない・無音・範囲外は**全て不合格**で、黙って通す経路は無い。
許容範囲は ``contracts.music_render`` の定数（唯一の定義。実素材で校正した値ではない）。
"""

from __future__ import annotations

import re
from decimal import Decimal

from contracts.music_render import (
    BGM_LOUDNESS_CHECK_NAME,
    LOUDNESS_INTEGRATED_MAX_CENTI,
    LOUDNESS_INTEGRATED_MIN_CENTI,
    LOUDNESS_TRUE_PEAK_MAX_CENTI,
    LoudnessMeasurement,
    LoudnessVerdict,
)
from contracts.render import TechnicalQaCheck

#: ebur128 が無音（ゲート未通過）で返す積分ラウドネスの下限値
SILENT_INTEGRATED_CENTI = -7000

_NUM = r"(-?inf|-?[0-9]+(?:\.[0-9]+)?)"
_INTEGRATED = re.compile(rf"Integrated loudness:\s*\n\s*I:\s*{_NUM} LUFS")
_LRA = re.compile(rf"Loudness range:\s*\n\s*LRA:\s*{_NUM} LU")
_PEAK = re.compile(rf"True peak:\s*\n\s*Peak:\s*{_NUM} dBFS")


class LoudnessParseError(ValueError):
    """要約が読めない。呼び出し側は不合格として扱う（通さない）。"""


def _centi(text: str) -> int | None:
    if text.endswith("inf"):
        return None
    return int((Decimal(text) * 100).to_integral_value())


def parse_ebur128_summary(stderr: str) -> LoudnessMeasurement:
    """最後の Summary 以降から I / LRA / Peak を取り出す。欠けていれば LoudnessParseError。"""
    index = stderr.rfind("Summary:")
    if index < 0:
        raise LoudnessParseError("ebur128 summary not found")
    tail = stderr[index:]
    found = [rx.search(tail) for rx in (_INTEGRATED, _LRA, _PEAK)]
    if not all(found):
        raise LoudnessParseError("ebur128 summary is incomplete")
    integrated, lra, peak = (_centi(m.group(1)) for m in found if m)
    return LoudnessMeasurement(
        integrated_lufs_centi=integrated, true_peak_dbtp_centi=peak, lra_centi=lra
    )


def evaluate_loudness(measured: LoudnessMeasurement) -> LoudnessVerdict:
    """範囲外は理由コードつきの不合格。優先: 無音 > 天井超過 > 大きすぎ > 小さすぎ。"""
    integrated = measured.integrated_lufs_centi
    peak = measured.true_peak_dbtp_centi
    reason: str
    if integrated is None or peak is None or integrated <= SILENT_INTEGRATED_CENTI:
        reason = "silent"
    elif peak > LOUDNESS_TRUE_PEAK_MAX_CENTI:
        reason = "true_peak_exceeded"
    elif integrated > LOUDNESS_INTEGRATED_MAX_CENTI:
        reason = "too_loud"
    elif integrated < LOUDNESS_INTEGRATED_MIN_CENTI:
        reason = "too_quiet"
    else:
        reason = "ok"
    return LoudnessVerdict(
        passed=reason == "ok",
        reason_code=reason,  # type: ignore[arg-type]
        measured=measured,
        integrated_min_centi=LOUDNESS_INTEGRATED_MIN_CENTI,
        integrated_max_centi=LOUDNESS_INTEGRATED_MAX_CENTI,
        true_peak_max_centi=LOUDNESS_TRUE_PEAK_MAX_CENTI,
    )


def loudness_check(verdict: LoudnessVerdict) -> TechnicalQaCheck:
    """技術 QA の 1 項目（``bgm_loudness``）。detail に理由コードと数値を残す。"""
    m = verdict.measured
    detail = (
        f"{verdict.reason_code}: integrated={m.integrated_lufs_centi} "
        f"(allowed {verdict.integrated_min_centi}..{verdict.integrated_max_centi}) centi-LUFS, "
        f"true_peak={m.true_peak_dbtp_centi} (max {verdict.true_peak_max_centi}) centi-dBTP, "
        f"lra={m.lra_centi} centi-LU"
    )
    return TechnicalQaCheck(
        check=BGM_LOUDNESS_CHECK_NAME, passed=verdict.passed, detail=detail[:500]
    )


__all__ = [
    "SILENT_INTEGRATED_CENTI",
    "LoudnessParseError",
    "evaluate_loudness",
    "loudness_check",
    "parse_ebur128_summary",
]
