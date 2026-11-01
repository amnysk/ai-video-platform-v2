"""地図 1 枚の準備: 関門（正確さ）→ 描画 → 見た目の検査 → 証跡（ADR-0084 §3 / §5）。

正確さが PASS でない地図は**描かない**（誤った位置の画像を作らない）。描いた地図の見た目が
PASS でなければ、画像は証跡に残るが使われない。この関数は I/O をしない（フォントの bytes と
下地は呼び出し側が渡す）。同じ入力から同じ記録と同じ PNG。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from contracts.geo import GeoVerdict
from contracts.geo_scene import GEO_SCENE_SCHEMA_VERSION, GeoMapRecord, GeoMapSpec
from domain.geo.digest import sha256_of
from domain.geo.map_binding import verification_digest_of
from domain.geo.map_gate import gate_map
from domain.geo.map_presentation import check_map_presentation, combine_map_results
from infrastructure.geo.source import GeoSourceData, parse_basemap
from infrastructure.media.map_render import MapRenderSpec, render_map


@dataclass(frozen=True, slots=True)
class PreparedMap:
    record: GeoMapRecord
    #: PASS のときだけ。使われる画像
    png: bytes | None

    @property
    def applied(self) -> bool:
        return self.record.verdict is GeoVerdict.PASS and self.png is not None


def prepare_map(
    spec: GeoMapSpec,
    data: GeoSourceData,
    render_spec: MapRenderSpec,
    font_bytes: bytes | None,
    *,
    render_profile_id: str | None = None,
) -> PreparedMap:
    outcome = gate_map(spec, data.gazetteer, data.claims, data.sources)
    manifest = outcome.manifest
    manifest_sha = sha256_of(manifest) if manifest is not None else None
    scene_id = outcome.accuracy.scene_id
    common = {
        "schema_version": GEO_SCENE_SCHEMA_VERSION,
        "scene_id": scene_id,
        "storyboard_scene_id": spec.storyboard_scene_id,
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "gazetteer_sha256": outcome.gazetteer_sha256,
        "evidence_sha256": outcome.evidence_sha256,
        "accuracy": outcome.accuracy,
    }
    if not outcome.passed or manifest is None:
        return PreparedMap(
            record=GeoMapRecord.model_validate(
                {
                    **common,
                    "verdict": outcome.accuracy.verdict,
                    "verification_sha256": verification_digest_of(outcome.accuracy, None),
                }
            ),
            png=None,
        )
    base = None
    if data.basemap_bytes is not None:
        base = parse_basemap(
            data.basemap_bytes,
            manifest.bbox,
            sha256=hashlib.sha256(data.basemap_bytes).hexdigest(),
        )
    rendered = render_map(manifest, render_spec, font_bytes, base)
    presentation = check_map_presentation(rendered.layout)
    combined = combine_map_results(
        outcome.accuracy,
        presentation,
        manifest_sha256=rendered.manifest_sha256,
        render_sha256=rendered.sha256,
    )
    assert rendered.manifest_sha256 == manifest_sha  # 描画器と記録が同じ manifest の指紋を使う
    record = GeoMapRecord.model_validate(
        {
            **common,
            "verdict": combined.verdict,
            "basemap_sha256": base.sha256 if base is not None else None,
            "presentation": presentation,
            "verification_sha256": verification_digest_of(outcome.accuracy, presentation),
            "png_sha256": rendered.sha256,
            "render_identity_sha256": rendered.identity_sha256,
            "font_id": rendered.font_id[:100],
            "render_profile_id": render_profile_id,
        }
    )
    return PreparedMap(
        record=record, png=rendered.png if combined.verdict is GeoVerdict.PASS else None
    )


__all__ = ["PreparedMap", "prepare_map"]
