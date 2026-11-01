"""BGM の品質検査（ADR-0083 §8、INV-138）。純粋関数。時計は ``now`` で注入、I/O は無い。

品質ゲートの検査列（``contracts.quality.QualityCheck``）を作る。gate は MUSIC と LICENSE。
永続化と gate への接続は別の枝（``claude/v3-p3-quality``）の責務で、ここは判定だけ。

- BGM を使っていない（``evidence is None``）なら、**その final_video に結び付いた**
  「使っていない」PASS を明示的に返す（``evidence_ref`` = final_video の sha256）。
  検査が無いことを PASS にしない（INV-101）。呼び出し側は「証跡が無い」と「証跡を読めなかった」を
  区別し、後者は例外にして gate を fail-closed にすること（ここは「無い」だけを受け取る）
- 使っているなら、証跡が今の final_video に結び付くこと・実測ラウドネスが範囲内・
  発話中の声と BGM の差が閾値以上・曲ごとのライセンスが**今**も有効で
  証拠 hash と音源 sha が一致・クレジット原文が一致、を検査する。
  1 つでも崩れたら BLOCK（差が未測定だけは REVIEW）
- detail は固定の ASCII 文だけ（入力の文字列を持たない。INV-106）
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from contracts.music import BgmTrack, VideoFormat
from contracts.music_render import (
    LOUDNESS_INTEGRATED_MAX_CENTI,
    LOUDNESS_INTEGRATED_MIN_CENTI,
    LOUDNESS_TRUE_PEAK_MAX_CENTI,
    BgmRenderEvidence,
    LoudnessMeasurement,
    SpeechIntelligibility,
)
from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from domain.music.selection import reject_reasons

#: 無音（ゲート未通過）の積分ラウドネス。``domain.render.loudness.SILENT_INTEGRATED_CENTI``
#: と同じ値で、一致は ``tests/unit/test_bgm_quality_checks.py`` が検査する
#: （domain/music は domain/render を import しない）
_SILENT_INTEGRATED_CENTI = -7000


def _check(
    gate: QualityGate,
    ok: bool,
    reason: QualityReason,
    detail_ok: str,
    detail_ng: str,
    evidence_ref: str,
    *,
    verdict_ng: QualityVerdict = QualityVerdict.BLOCK,
) -> QualityCheck:
    return QualityCheck(
        gate=gate,
        verdict=QualityVerdict.PASS if ok else verdict_ng,
        reason_code=QualityReason.OK if ok else reason,
        detail=detail_ok if ok else detail_ng,
        deterministic=True,
        evidence_ref=evidence_ref,
    )


def _loudness_in_range(m: LoudnessMeasurement) -> bool:
    """``domain.render.loudness.evaluate_loudness`` と同じ判定（一致は単体テストで検査）。"""
    i, p = m.integrated_lufs_centi, m.true_peak_dbtp_centi
    if i is None or p is None or i <= _SILENT_INTEGRATED_CENTI:
        return False
    return (
        p <= LOUDNESS_TRUE_PEAK_MAX_CENTI
        and LOUDNESS_INTEGRATED_MIN_CENTI <= i <= LOUDNESS_INTEGRATED_MAX_CENTI
    )


def _not_used(final_video_sha256: str) -> tuple[QualityCheck, ...]:
    return tuple(
        QualityCheck(
            gate=gate,
            verdict=QualityVerdict.PASS,
            reason_code=QualityReason.OK,
            detail="bgm not used in this final video",
            deterministic=True,
            evidence_ref=final_video_sha256,
        )
        for gate in (QualityGate.MUSIC, QualityGate.LICENSE)
    )


def _unbound(ref: str) -> tuple[QualityCheck, ...]:
    return (
        _check(
            QualityGate.MUSIC,
            False,
            QualityReason.AUDIO_DEFECT,
            "",
            "bgm evidence does not belong to this final video",
            ref,
        ),
        _check(
            QualityGate.LICENSE,
            False,
            QualityReason.LICENSE_UNVERIFIED,
            "",
            "bgm evidence does not belong to this final video",
            ref,
        ),
    )


def bgm_quality_checks(
    *,
    final_video_sha256: str,
    evidence: BgmRenderEvidence | None,
    evidence_sha256: str | None,
    catalog: Iterable[BgmTrack],
    now: datetime,
    intelligibility: SpeechIntelligibility | None,
    video_format: VideoFormat | None,
) -> tuple[QualityCheck, ...]:
    """BGM の検査列。

    ``evidence``: その final_video の ``BgmRenderEvidence``（無ければ ``None`` = BGM 不使用）。
    ``evidence_sha256``: 証跡オブジェクトの sha256（保存時に読み戻して検証済みの値。
    ``evidence`` があれば必須）。
    ``catalog``: **今の**カタログ（描画後に失効・取消された曲を見つけるため）。
    ``intelligibility``: 発話中の声と BGM の差の実測（未測定は ``None``）。
    ``video_format``: 動画の形式（Shorts 不可の曲を通さないため。
    不明なら ``None`` = 形式は見ない）。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if evidence is None:
        return _not_used(final_video_sha256)
    if evidence_sha256 is None:
        raise ValueError("evidence_sha256 is required when evidence is given")
    ref = evidence_sha256
    if evidence.final_video_sha256 != final_video_sha256:
        return _unbound(ref)

    by_id = {t.track_id: t for t in catalog}
    music: list[QualityCheck] = [
        _check(
            QualityGate.MUSIC,
            _loudness_in_range(evidence.loudness.measured),
            QualityReason.AUDIO_DEFECT,
            "bgm mix loudness within range",
            "bgm mix loudness out of range",
            ref,
        )
    ]
    if intelligibility is None:
        music.append(
            _check(
                QualityGate.MUSIC,
                False,
                QualityReason.AUDIO_DEFECT,
                "",
                "speech margin over bgm not measured",
                ref,
                verdict_ng=QualityVerdict.REVIEW,
            )
        )
    else:
        music.append(
            _check(
                QualityGate.MUSIC,
                intelligibility.passed,
                QualityReason.AUDIO_DEFECT,
                "narration stays above bgm by the required margin",
                "narration margin over bgm below the required level",
                ref,
            )
        )

    license_checks: list[QualityCheck] = []
    expected_credits: list[str] = []
    for used in evidence.tracks:
        track = by_id.get(used.track_id)
        usable = (
            track is not None
            and not reject_reasons(track, now=now, fmt=video_format, platform="youtube")
            and track.file_sha256 == used.source_sha256
            and track.license_evidence_sha256 == used.license_evidence_hash
        )
        license_checks.append(
            _check(
                QualityGate.LICENSE,
                usable,
                QualityReason.LICENSE_UNVERIFIED,
                "bgm track license is valid and matches its evidence",
                "bgm track license is not valid or does not match its evidence",
                ref,
            )
        )
        if (
            track is not None
            and track.attribution_required
            and track.exact_attribution_text is not None
            and track.exact_attribution_text not in expected_credits
        ):
            expected_credits.append(track.exact_attribution_text)
    license_checks.append(
        _check(
            QualityGate.LICENSE,
            tuple(expected_credits) == evidence.credit_lines,
            QualityReason.LICENSE_UNVERIFIED,
            "bgm credit lines equal the catalog text",
            "bgm credit lines differ from the catalog text",
            ref,
        )
    )
    return (*music, *license_checks)


__all__ = ["bgm_quality_checks"]
