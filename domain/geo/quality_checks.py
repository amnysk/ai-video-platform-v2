"""地図の品質検査（``QualityGate.GEO``）への変換（ADR-0084 §6、INV-142）。純関数。

QualityGate の永続化・投稿可否は別の枝（``domain/quality`` と配線）が持つ。この関数は
**地図の証跡（``GeoMapRecord``）を ``QualityCheck`` の列にするだけ**で、I/O も時計も無い。

- 地図を使っていない Episode は、完成動画の sha256 に結びついた**明示の PASS**を返す
  （「検査していない」と「地図が無かった」を区別する。証跡 ``evidence_ref`` は
  ``{"geo": "not_used", "final_video_sha256": ...}`` の sha256）。
- 地図を指定したのに証跡が無い・複数ある・hash の鎖が切れている・別の完成動画の証跡は BLOCK。
- BLOCK / REVIEW の地図は投稿を許さない（位置違い・根拠不足・時代錯誤は ``GEO_MISMATCH``、
  時間基準が不明なだけなら ``GEO_TIME_BASIS_UNKNOWN``、見た目だけの欠陥は ``VISUAL_DEFECT``）。
- 入力の文字列（地名・主張）は ``detail`` に入れない。理由コード（enum の値）だけ。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence

from contracts.geo import GeoVerdict
from contracts.geo_map import MapReasonCode
from contracts.geo_scene import GeoMapRecord
from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from domain.geo.digest import sha256_of
from domain.geo.map_binding import binding_problems, record_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CODES_IN_DETAIL = 3


def not_used_evidence_ref(final_video_sha256: str) -> str:
    """「この完成動画に地図は使われていない」の証跡 id（完成動画に結びつく）。"""
    return sha256_of({"geo": "not_used", "final_video_sha256": final_video_sha256})


def _check(
    verdict: GeoVerdict,
    reason: QualityReason,
    detail: str,
    *,
    scene_id: str | None,
    evidence_ref: str | None,
) -> QualityCheck:
    return QualityCheck(
        gate=QualityGate.GEO,
        verdict=QualityVerdict(verdict.value),
        reason_code=reason,
        detail=detail,
        deterministic=True,
        scene_id=scene_id,
        evidence_ref=evidence_ref,
    )


def _codes(record: GeoMapRecord) -> list[str]:
    seen: list[str] = []
    for f in record.accuracy.findings:
        if f.reason_code is not MapReasonCode.OK and f.reason_code.value not in seen:
            seen.append(f.reason_code.value)
    if record.presentation is not None:
        for pf in record.presentation.findings:
            if pf.reason_code.value != "ok" and pf.reason_code.value not in seen:
                seen.append(pf.reason_code.value)
    return seen


def _record_check(record: GeoMapRecord, final_video_sha256: str) -> QualityCheck:
    ref = record_sha256(record)
    scene = record.scene_id
    problems = binding_problems(record, final_video_sha256=final_video_sha256)
    if problems:
        names = ",".join(p.value for p in problems[:_MAX_CODES_IN_DETAIL])
        return _check(
            GeoVerdict.BLOCK,
            QualityReason.GEO_MISMATCH,
            f"map evidence is not bound to this video: {names}",
            scene_id=scene,
            evidence_ref=ref,
        )
    if record.verdict is GeoVerdict.PASS:
        return _check(
            GeoVerdict.PASS,
            QualityReason.OK,
            "map verified and bound to the final video",
            scene_id=scene,
            evidence_ref=ref,
        )
    codes = _codes(record)
    accuracy_codes = {
        f.reason_code for f in record.accuracy.findings if f.reason_code is not MapReasonCode.OK
    }
    if accuracy_codes and accuracy_codes <= {MapReasonCode.AS_OF_UNKNOWN}:
        reason = QualityReason.GEO_TIME_BASIS_UNKNOWN
    elif accuracy_codes:
        reason = QualityReason.GEO_MISMATCH
    else:
        reason = QualityReason.VISUAL_DEFECT
    shown = ",".join(codes[:_MAX_CODES_IN_DETAIL])
    return _check(
        record.verdict,
        reason,
        f"map refused: {shown}" if shown else "map refused",
        scene_id=scene,
        evidence_ref=ref,
    )


def map_quality_checks(
    *,
    final_video_sha256: str,
    records: Sequence[GeoMapRecord],
    intended_scene_ids: Collection[str] = (),
) -> tuple[QualityCheck, ...]:
    """地図の品質検査。順序は scene_id 昇順で決定的。空の列は返さない。

    ``intended_scene_ids`` は「このエピソードが地図を使うと指定した台本シーン」。呼び出し側は
    地図の指定（``GeoMapSpecs``）から導く。指定があるのに証跡が無いシーンは BLOCK。
    """
    if not _SHA256.fullmatch(final_video_sha256):
        raise ValueError("final_video_sha256 must be a lowercase sha256 hex")
    intended = set(intended_scene_ids)
    by_scene: dict[str, list[GeoMapRecord]] = {}
    for record in records:
        by_scene.setdefault(record.scene_id, []).append(record)
    if not by_scene and not intended:
        return (
            _check(
                GeoVerdict.PASS,
                QualityReason.OK,
                "no map was used in this episode",
                scene_id=None,
                evidence_ref=not_used_evidence_ref(final_video_sha256),
            ),
        )
    checks: list[QualityCheck] = []
    for scene_id in sorted(intended | set(by_scene)):
        found = by_scene.get(scene_id, [])
        if not found:
            checks.append(
                _check(
                    GeoVerdict.BLOCK,
                    QualityReason.GEO_MISMATCH,
                    "a map was specified but there is no map evidence",
                    scene_id=scene_id,
                    evidence_ref=None,
                )
            )
        elif len(found) > 1:
            checks.append(
                _check(
                    GeoVerdict.BLOCK,
                    QualityReason.GEO_MISMATCH,
                    "more than one map evidence for the same scene",
                    scene_id=scene_id,
                    evidence_ref=record_sha256(found[0]),
                )
            )
        else:
            checks.append(_record_check(found[0], final_video_sha256))
    return tuple(checks)


__all__ = ["map_quality_checks", "not_used_evidence_ref"]
