"""地図の歴史的正確さの判定（ADR-0078、INV-117）。純関数（INV-6）。

Wave 1 の検証器（``verify_manifest`` / ``verify_scene``）の結果を**作り直さず**、
QualityGate が読める構造化された結果（verdict + 理由コード + 対象 id + 根拠の参照）に写す。
自由文は出さない。

検証器との違いは 1 点だけ: 歴史地図で**誤った史実をそのまま提示する**種類の REVIEW を BLOCK にする
（アナクロ・現代国境・根拠の欠落。``ESCALATED_REASONS``）。同名異所・参照なし・時間基準不明のような
「判断材料が足りない」REVIEW は REVIEW のまま（自動で 1 つに決めない: ADR-0071）。
ここで verdict を**緩める**経路は無い（``escalate`` は REVIEW → BLOCK の一方向）。
視認性（presentation）はこの結果に影響しない（別の関数・別の理由コード）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import ValidationError

from contracts.geo import (
    GEO_VERDICT_ORDER,
    MAX_CHECKS,
    BorderBasis,
    GeoCheck,
    GeoManifest,
    GeoPlace,
    GeoVerdict,
    GeoVerification,
    MapKind,
    ReferencePlace,
    stricter,
)
from contracts.geo_map import (
    MapAccuracyResult,
    MapFinding,
    MapReasonCode,
    MapTargetKind,
)
from contracts.research_evidence import EvidenceClaim, EvidenceSource
from domain.geo.verify import resolve_reference_candidates, verify_manifest, verify_scene

R = MapReasonCode

#: finding の上限。超える分は厳しい順に残して打ち切り、TOO_MANY_FINDINGS の BLOCK を 1 件足す。
MAX_FINDINGS = MAX_CHECKS - 1

#: 歴史地図で REVIEW → BLOCK にする理由（時代・国境の誤りを史実として提示する）。定義はここだけ。
ESCALATED_ON_HISTORICAL: frozenset[MapReasonCode] = frozenset(
    {
        R.ANACHRONISTIC_PLACE,
        R.ANACHRONISTIC_NAME,
        R.ANACHRONISTIC_LABEL,
        R.MODERN_BORDER,
        R.BORDER_ERA_MISMATCH,
        R.BORDER_UNSUPPORTED,
    }
)
#: 模式図以外の全ての地図で REVIEW → BLOCK にする理由（根拠に結びつかない位置を提示しない）。
ESCALATED_ALWAYS: frozenset[MapReasonCode] = frozenset(
    {R.EVIDENCE_MISSING, R.SOURCE_MISSING, R.EVIDENCE_CONFLICT}
)

#: 検証器の地点ごとの check（``<place_id>.<suffix>``）→ 理由コード。
_PLACE_SUFFIX: dict[str, MapReasonCode] = {
    "bounds": R.OUT_OF_BOUNDS,
    "uncertainty": R.UNCERTAINTY_OVERSTATED,
    "swap": R.AXES_SWAPPED,
    "distance": R.POSITION_MISMATCH,
    "name": R.NAME_MISMATCH,
    "valid_time": R.ANACHRONISTIC_PLACE,
    "name_era": R.ANACHRONISTIC_NAME,
    "claim": R.EVIDENCE_MISSING,
    "claim_relevance": R.EVIDENCE_NOT_RELEVANT,
    "sources": R.SOURCE_MISSING,
}
#: manifest 全体の check 名 → 理由コード（``borders`` は manifest の宣言から決める）。
_MANIFEST_NAMES: dict[str, MapReasonCode] = {
    "as_of": R.AS_OF_UNKNOWN,
    "places": R.NO_PLACES,
    "schematic_disclaimer": R.SCHEMATIC_DISCLAIMER_MISSING,
    "manifest_required": R.MANIFEST_REQUIRED,
    "schematic_claims_real_location": R.SCHEMATIC_AS_REAL,
    "claims.duplicate": R.EVIDENCE_CONFLICT,
}


def escalate(code: MapReasonCode, outcome: GeoVerdict, kind: MapKind | None) -> GeoVerdict:
    """REVIEW を BLOCK に引き上げる（**緩める方向には動かない**）。"""
    if outcome is not GeoVerdict.REVIEW:
        return outcome
    if code in ESCALATED_ALWAYS and kind is not MapKind.SCHEMATIC:
        return GeoVerdict.BLOCK
    if code in ESCALATED_ON_HISTORICAL and kind is MapKind.HISTORICAL:
        return GeoVerdict.BLOCK
    return outcome


def _border_code(manifest: GeoManifest) -> MapReasonCode:
    if manifest.borders is BorderBasis.MODERN:
        return R.MODERN_BORDER
    if manifest.borders is BorderBasis.HISTORICAL:
        return R.BORDER_ERA_MISMATCH
    return R.BORDER_UNSUPPORTED


def _classify(
    check: GeoCheck,
    manifest: GeoManifest | None,
    scene_id: str,
    references: Sequence[ReferencePlace],
) -> MapFinding | None:
    """check 1 件 → finding。PASS は ``None``（finding にしない）。"""
    if check.outcome is GeoVerdict.PASS:
        return None
    kind = manifest.map_kind if manifest is not None else None
    places = {p.place_id: p for p in manifest.places} if manifest is not None else {}

    code: MapReasonCode | None = None
    target_kind, target_id = MapTargetKind.MANIFEST, scene_id
    place: GeoPlace | None = None

    if check.name in _MANIFEST_NAMES:
        code = _MANIFEST_NAMES[check.name]
    elif check.name == "borders" and manifest is not None:
        code = _border_code(manifest)
    elif check.name.startswith("label[") and check.name.endswith("]"):
        label_pid = check.name[len("label[") : -1]
        target_kind, target_id = MapTargetKind.LABEL, label_pid
        place = places.get(label_pid)
        code = R.LABEL_MISMATCH if check.outcome is GeoVerdict.BLOCK else R.ANACHRONISTIC_LABEL
    else:
        for pid, candidate in places.items():
            prefix = pid + "."
            if check.name.startswith(prefix) and check.name[len(prefix) :] in _PLACE_SUFFIX:
                code = _PLACE_SUFFIX[check.name[len(prefix) :]]
                target_kind, target_id, place = MapTargetKind.PLACE, pid, candidate
                break
            if check.name == pid + ".reference":
                n = len(resolve_reference_candidates(candidate, references))
                code = R.UNKNOWN_PLACE if n == 0 else R.AMBIGUOUS_PLACE
                target_kind, target_id, place = MapTargetKind.PLACE, pid, candidate
                break
    if code is None:
        code = R.UNCLASSIFIED
    return MapFinding(
        verdict=escalate(code, check.outcome, kind),
        reason_code=code,
        target_kind=target_kind,
        target_id=target_id,
        evidence_claim_id=place.claim_id if place is not None else None,
        evidence_source_ids=place.source_ids if place is not None else (),
    )


def _result(
    scene_id: str,
    checks: Sequence[GeoCheck],
    manifest: GeoManifest | None,
    references: Sequence[ReferencePlace],
) -> MapAccuracyResult:
    findings = [
        f for check in checks if (f := _classify(check, manifest, scene_id, references)) is not None
    ]
    if not findings:
        findings = [
            MapFinding(
                verdict=GeoVerdict.PASS,
                reason_code=R.OK,
                target_kind=MapTargetKind.MANIFEST,
                target_id=scene_id,
            )
        ]
    if len(findings) > MAX_FINDINGS:
        order = {v: i for i, v in enumerate(reversed(GEO_VERDICT_ORDER))}
        findings = sorted(findings, key=lambda f: order[f.verdict])[:MAX_FINDINGS]  # 安定ソート
        findings.append(_too_many(scene_id))
    worst = GeoVerdict.PASS
    for f in findings:
        worst = stricter(worst, f.verdict)
    return MapAccuracyResult(scene_id=scene_id, verdict=worst, findings=tuple(findings))


def _too_many(scene_id: str) -> MapFinding:
    return MapFinding(
        verdict=GeoVerdict.BLOCK,
        reason_code=R.TOO_MANY_FINDINGS,
        target_kind=MapTargetKind.MANIFEST,
        target_id=scene_id,
    )


def _verify_total(
    scene_id: str, run: Callable[[], GeoVerification]
) -> GeoVerification | MapAccuracyResult:
    """検証器の結果が契約の上限で作れないときも例外にせず BLOCK の結果にする（全域関数）。"""
    try:
        return run()
    except ValidationError:
        finding = _too_many(scene_id)
        return MapAccuracyResult(scene_id=scene_id, verdict=GeoVerdict.BLOCK, findings=(finding,))


def check_map_accuracy(
    manifest: GeoManifest,
    reference_places: Sequence[ReferencePlace],
    evidence_claims: Sequence[EvidenceClaim],
    evidence_sources: Sequence[EvidenceSource] | None = None,
) -> MapAccuracyResult:
    """manifest の歴史的正確さ。QualityGate が読む入口（決定論・LLM なし）。

    ``evidence_sources`` を渡さないと ``source_ids`` が取得済みの資料に解決するかは検査されない。
    本番の呼び出しは渡すこと（ADR-0078）。
    """
    verification = _verify_total(
        manifest.scene_id,
        lambda: verify_manifest(manifest, reference_places, evidence_claims, evidence_sources),
    )
    if isinstance(verification, MapAccuracyResult):
        return verification
    return _result(manifest.scene_id, verification.checks, manifest, reference_places)


def check_scene_map_accuracy(
    claims_real_location: bool,
    manifest: GeoManifest | None,
    scene_id: str,
    reference_places: Sequence[ReferencePlace],
    evidence_claims: Sequence[EvidenceClaim],
    evidence_sources: Sequence[EvidenceSource] | None = None,
) -> MapAccuracyResult:
    """INV-93 込みの入口。実在の位置を主張するシーンに manifest が無ければ BLOCK。"""
    sid = manifest.scene_id if manifest is not None else scene_id
    verification = _verify_total(
        sid,
        lambda: verify_scene(
            claims_real_location, manifest, reference_places, evidence_claims, evidence_sources
        ),
    )
    if isinstance(verification, MapAccuracyResult):
        return verification
    return _result(sid, verification.checks, manifest, reference_places)


__all__ = [
    "MAX_FINDINGS",
    "ESCALATED_ALWAYS",
    "ESCALATED_ON_HISTORICAL",
    "check_map_accuracy",
    "check_scene_map_accuracy",
    "escalate",
]
