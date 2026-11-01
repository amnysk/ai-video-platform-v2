"""保存済みの技術 QA（``TechnicalQaReport``）を VISUAL / AUDIO の gate の検査にする（ADR-0081 §3）。

純粋。技術 QA は完成動画 JSON の中にあり、JSON の正準 hash が対象 hash なので、呼び出し側が hash を
確かめてから渡せば**対象に結び付いた**入力になる。分け方の表はここだけ（AGENTS.md §8）。
必須の検査名（``TECHNICAL_QA_CHECK_NAMES``）が 1 つでも欠ける・不合格なら、その側の gate は BLOCK。
空洞化した報告（1 項目だけ等）を PASS にしない。
"""

from __future__ import annotations

from contracts.music_render import BGM_LOUDNESS_CHECK_NAME
from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from contracts.render import TechnicalQaReport
from domain.render.qa import (
    CHECK_AUDIO_CHANNELS,
    CHECK_AUDIO_CODEC,
    CHECK_AUDIO_DURATION,
    CHECK_AUDIO_PRESENT,
    CHECK_AUDIO_SAMPLE_RATE,
    CHECK_VOICE_COVERAGE,
    TECHNICAL_QA_CHECK_NAMES,
)

#: AUDIO gate の検査名。これ以外（映像・字幕・網羅・読み戻し）は VISUAL gate。
AUDIO_CHECK_NAMES: frozenset[str] = frozenset(
    {
        CHECK_AUDIO_PRESENT,
        CHECK_AUDIO_CODEC,
        CHECK_AUDIO_SAMPLE_RATE,
        CHECK_AUDIO_CHANNELS,
        CHECK_AUDIO_DURATION,
        CHECK_VOICE_COVERAGE,
        BGM_LOUDNESS_CHECK_NAME,
    }
)

_REQUIRED_AUDIO = frozenset(n for n in TECHNICAL_QA_CHECK_NAMES if n in AUDIO_CHECK_NAMES)
_REQUIRED_VISUAL = frozenset(n for n in TECHNICAL_QA_CHECK_NAMES if n not in AUDIO_CHECK_NAMES)

DETAIL_PASSED = "technical qa recorded and all required checks passed"
DETAIL_INCOMPLETE = "technical qa is missing required checks"
DETAIL_FAILED = "technical qa has a failed check"


def _gate_check(
    gate: QualityGate, present: dict[str, bool], required: frozenset[str], defect: QualityReason
) -> QualityCheck:
    if not required <= present.keys():
        detail, ok = DETAIL_INCOMPLETE, False
    elif not all(present[n] for n in required):
        detail, ok = DETAIL_FAILED, False
    else:
        detail, ok = DETAIL_PASSED, True
    return QualityCheck(
        gate=gate,
        verdict=QualityVerdict.PASS if ok else QualityVerdict.BLOCK,
        reason_code=QualityReason.OK if ok else defect,
        detail=detail,
        deterministic=True,
    )


def technical_qa_checks(report: TechnicalQaReport) -> tuple[QualityCheck, ...]:
    """VISUAL と AUDIO の検査を 1 つずつ返す（``CheckKey.TECHNICAL_QA`` の gate 集合と同じ）。"""
    result = {c.check: c.passed for c in report.checks}
    audio = {n: p for n, p in result.items() if n in AUDIO_CHECK_NAMES}
    visual = {n: p for n, p in result.items() if n not in AUDIO_CHECK_NAMES}
    return (
        _gate_check(QualityGate.VISUAL, visual, _REQUIRED_VISUAL, QualityReason.VISUAL_DEFECT),
        _gate_check(QualityGate.AUDIO, audio, _REQUIRED_AUDIO, QualityReason.AUDIO_DEFECT),
    )
