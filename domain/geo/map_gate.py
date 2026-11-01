"""地図 1 枚の「作れるか・正確か」の関門（ADR-0084 §3）。純関数。決定論・LLM なし。

``GeoMapSpec``（依頼か manifest）→ gazetteer と根拠（claim / source）に照らした
``MapAccuracyResult``。BLOCK / REVIEW の地図は描かれない・使われない（呼び出し側が
``accuracy.verdict`` を見る。ここは判定だけで I/O をしない）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from contracts.geo import GeoManifest, GeoVerdict, ReferencePlace
from contracts.geo_map import MapAccuracyResult
from contracts.geo_scene import GeoMapSpec
from contracts.research_evidence import EvidenceClaim, EvidenceSource
from domain.geo.digest import sha256_of
from domain.geo.manifest_builder import build_manifest
from domain.geo.map_accuracy import check_map_accuracy


@dataclass(frozen=True, slots=True)
class GateOutcome:
    manifest: GeoManifest | None
    accuracy: MapAccuracyResult
    gazetteer_sha256: str
    evidence_sha256: str

    @property
    def passed(self) -> bool:
        return self.accuracy.verdict is GeoVerdict.PASS and self.manifest is not None


def gazetteer_digest(gazetteer: Sequence[ReferencePlace]) -> str:
    """順序に依存しない gazetteer の指紋（ref_id 昇順）。"""
    rows = [r.model_dump(mode="json") for r in sorted(gazetteer, key=lambda r: r.ref_id)]
    return sha256_of({"gazetteer": rows})


def evidence_digest(claims: Sequence[EvidenceClaim], sources: Sequence[EvidenceSource]) -> str:
    cs = [c.model_dump(mode="json") for c in sorted(claims, key=lambda c: c.claim_id)]
    ss = [s.model_dump(mode="json") for s in sorted(sources, key=lambda s: s.source_id)]
    return sha256_of({"claims": cs, "sources": ss})


def gate_map(
    spec: GeoMapSpec,
    gazetteer: Sequence[ReferencePlace],
    claims: Sequence[EvidenceClaim],
    sources: Sequence[EvidenceSource],
) -> GateOutcome:
    """資料の取得結果（``sources``）を**必ず**渡して検証する（渡さないと出典の解決を見ない）。"""
    g_sha, e_sha = gazetteer_digest(gazetteer), evidence_digest(claims, sources)
    if spec.manifest is not None:
        manifest: GeoManifest | None = spec.manifest
        accuracy = check_map_accuracy(spec.manifest, gazetteer, claims, sources)
    else:
        assert spec.request is not None
        manifest, findings = build_manifest(spec.request, gazetteer)
        if manifest is None:
            # build_manifest は manifest を作れないとき必ず理由を返す（空なら契約違反）
            assert findings
            accuracy = MapAccuracyResult(
                scene_id=spec.request.scene_id, verdict=GeoVerdict.BLOCK, findings=findings
            )
        else:
            accuracy = check_map_accuracy(manifest, gazetteer, claims, sources)
    return GateOutcome(
        manifest=manifest, accuracy=accuracy, gazetteer_sha256=g_sha, evidence_sha256=e_sha
    )


__all__ = ["GateOutcome", "evidence_digest", "gate_map", "gazetteer_digest"]
