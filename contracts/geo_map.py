"""地図の「歴史的正確さ」の判定結果の契約（ADR-0078）。

``contracts/geo.py``（GeoManifest と検証の契約）には触れず、その上に載る**構造化された結果**だけを
ここで宣言する。QualityGate が受け取る形: verdict + 理由コード + 対象 id + 根拠の参照。
**自由文を持たない**（LLM / manifest 由来の文字列を結果へ流さない。対象 id は契約の pattern、
根拠は claim_id / source_id の pattern で縛られる）。

この契約はどの workflow・worker にも接続されていない（ADR-0078 §未接続）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from contracts.artifact_refs import SCRIPT_SCENE_ID_PATTERN, FrozenModel
from contracts.geo import MAX_CHECKS, PLACE_ID_PATTERN, GeoVerdict, stricter
from contracts.research_evidence import CLAIM_ID_PATTERN, SOURCE_ID_PATTERN

#: manifest 全体が対象の finding の ``target_id``（scene_id）/ 地点・ラベルが対象なら place_id。
TARGET_ID_PATTERN = rf"(?:{SCRIPT_SCENE_ID_PATTERN[1:-1]})|(?:{PLACE_ID_PATTERN[1:-1]})"


class MapTargetKind(StrEnum):
    MANIFEST = "manifest"
    PLACE = "place"
    LABEL = "label"


class MapReasonCode(StrEnum):
    """歴史的正確さの理由コード（**唯一の定義**）。``OK`` 以外が非 PASS の理由になる。"""

    OK = "ok"
    # 位置
    POSITION_MISMATCH = "position_mismatch"
    AXES_SWAPPED = "axes_swapped"
    OUT_OF_BOUNDS = "out_of_bounds"
    UNCERTAINTY_OVERSTATED = "uncertainty_overstated"
    # 同定
    AMBIGUOUS_PLACE = "ambiguous_place"
    UNKNOWN_PLACE = "unknown_place"
    NAME_MISMATCH = "name_mismatch"
    LABEL_MISMATCH = "label_mismatch"
    # 時代
    AS_OF_UNKNOWN = "as_of_unknown"
    ANACHRONISTIC_PLACE = "anachronistic_place"
    ANACHRONISTIC_NAME = "anachronistic_name"
    ANACHRONISTIC_LABEL = "anachronistic_label"
    # 国境
    MODERN_BORDER = "modern_border"
    BORDER_ERA_MISMATCH = "border_era_mismatch"
    BORDER_UNSUPPORTED = "border_unsupported"
    # 根拠
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_NOT_RELEVANT = "evidence_not_relevant"
    EVIDENCE_CONFLICT = "evidence_conflict"
    SOURCE_MISSING = "source_missing"
    # 構造・模式図
    MANIFEST_REQUIRED = "manifest_required"
    NO_PLACES = "no_places"
    SCHEMATIC_DISCLAIMER_MISSING = "schematic_disclaimer_missing"
    SCHEMATIC_AS_REAL = "schematic_as_real"
    #: 検証器の check の語彙に分類表が追いついていない。非 PASS のまま通報する（fail-closed）
    UNCLASSIFIED = "unclassified"


class MapFinding(FrozenModel):
    verdict: GeoVerdict
    reason_code: MapReasonCode
    target_kind: MapTargetKind
    target_id: str = Field(pattern=f"^(?:{TARGET_ID_PATTERN})$")
    #: 根拠の参照。claim_id / source_id の形だけ（本文・URL を持たない）
    evidence_claim_id: str | None = Field(default=None, pattern=CLAIM_ID_PATTERN)
    evidence_source_ids: tuple[Annotated[str, Field(pattern=SOURCE_ID_PATTERN)], ...] = ()

    @model_validator(mode="after")
    def _reason_matches_verdict(self) -> MapFinding:
        if (self.verdict is GeoVerdict.PASS) != (self.reason_code is MapReasonCode.OK):
            raise ValueError("reason_code 'ok' is for PASS findings only")
        return self


class MapAccuracyResult(FrozenModel):
    """歴史的正確さの結果。``verdict`` は finding の最悪値と一致しなければ作れない。"""

    scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    verdict: GeoVerdict
    findings: Annotated[tuple[MapFinding, ...], Field(min_length=1, max_length=MAX_CHECKS)]

    @model_validator(mode="after")
    def _verdict_is_the_worst_finding(self) -> MapAccuracyResult:
        worst = GeoVerdict.PASS
        for finding in self.findings:
            worst = stricter(worst, finding.verdict)
        if self.verdict is not worst:
            raise ValueError(
                f"verdict {self.verdict.value} does not match the worst finding ({worst.value})"
            )
        return self


__all__ = [
    "TARGET_ID_PATTERN",
    "MapAccuracyResult",
    "MapFinding",
    "MapReasonCode",
    "MapTargetKind",
]
