"""地図の指定・gazetteer・根拠・下地の供給元（ADR-0084 §2 / §9）。

非 DB の供給元（BGM の ``LocalDirectoryBgmSource`` と同じ形）: ``{root}/`` の JSON ファイル。
ネットワークに出ない。DB / MinIO 版への差し替え口は ``GeoSource`` の Protocol。

    {root}/gazetteer.json                gazetteer（``GazetteerFile``。license と curated_by 必須）
    {root}/basemap.geojson               下地の陸地（任意。Polygon / MultiPolygon の GeoJSON）
    {root}/{episode_id}/specs.json       この Episode の地図の指定（``GeoMapSpecs``）
    {root}/{episode_id}/evidence.json    根拠（research の ``EvidenceArtifact`` の JSON）

``specs.json`` が無い Episode は地図なし（``None``）。あるのに読めない・契約違反は
``GeoSourceInvalidError``（地図を使う意図を無かったことにしない）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from contracts.geo import GeoBBox, ReferencePlace
from contracts.geo_scene import GazetteerFile, GeoMapSpecs
from contracts.research_evidence import EvidenceClaim, EvidenceSource, parse_evidence_artifact
from domain.errors import GeoSourceInvalidError
from infrastructure.media.map_render import BaseMap

BASEMAP_CAPTION = "Modern coastline (Natural Earth)"


@dataclass(frozen=True, slots=True)
class GeoSourceData:
    specs: GeoMapSpecs
    gazetteer: tuple[ReferencePlace, ...]
    claims: tuple[EvidenceClaim, ...]
    sources: tuple[EvidenceSource, ...]
    #: 下地の陸地の生 bytes（無ければ ``None``）。sha256 は描画の identity と証跡に入る
    basemap_bytes: bytes | None = None


@runtime_checkable
class GeoSource(Protocol):
    """同期（呼び出し側がスレッドで待つ）。地図なしの Episode は ``None``。"""

    def load(self, episode_id: str) -> GeoSourceData | None: ...


def _read_json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise GeoSourceInvalidError(f"{what} is not readable: {type(exc).__name__}") from exc


class LocalDirectoryGeoSource:
    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, episode_id: str) -> GeoSourceData | None:
        if not episode_id or "/" in episode_id or episode_id.startswith("."):
            raise GeoSourceInvalidError("episode id is not a safe directory name")
        specs_path = self._root / episode_id / "specs.json"
        if not specs_path.is_file():
            return None
        try:
            specs = GeoMapSpecs.model_validate(_read_json(specs_path, "specs.json"))
            gazetteer_file = GazetteerFile.model_validate(
                _read_json(self._root / "gazetteer.json", "gazetteer.json")
            )
            evidence_raw = _read_json(self._root / episode_id / "evidence.json", "evidence.json")
            evidence = parse_evidence_artifact(evidence_raw)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise GeoSourceInvalidError(
                f"geo source violates its contract ({exc.error_count()} errors)"
            ) from exc
        basemap_path = self._root / "basemap.geojson"
        basemap_bytes: bytes | None = None
        if basemap_path.is_file():
            try:
                basemap_bytes = basemap_path.read_bytes()
            except OSError as exc:
                raise GeoSourceInvalidError("basemap.geojson is not readable") from exc
        return GeoSourceData(
            specs=specs,
            gazetteer=gazetteer_file.places,
            claims=evidence.claims,
            sources=evidence.sources,
            basemap_bytes=basemap_bytes,
        )


def parse_basemap(raw: bytes, bbox: GeoBBox, *, sha256: str) -> BaseMap:
    """GeoJSON の Polygon / MultiPolygon から、``bbox`` と重なる外周だけを取る（決定的な順序）。

    穴（内側のリング）は無視する（湖などは描かない）。座標は (lon, lat)。壊れた入力は
    ``GeoSourceInvalidError``。
    """
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise GeoSourceInvalidError("basemap is not JSON") from exc
    rings: list[tuple[tuple[float, float], ...]] = []

    def take(poly: object) -> None:
        if not isinstance(poly, list) or not poly or not isinstance(poly[0], list):
            raise GeoSourceInvalidError("basemap polygon is malformed")
        try:
            outer = tuple((float(x[0]), float(x[1])) for x in poly[0])
        except (TypeError, ValueError, IndexError) as exc:
            raise GeoSourceInvalidError("basemap ring is malformed") from exc
        lons = [p[0] for p in outer]
        lats = [p[1] for p in outer]
        if (
            max(lons) >= bbox.min_lon
            and min(lons) <= bbox.max_lon
            and max(lats) >= bbox.min_lat
            and min(lats) <= bbox.max_lat
        ):
            rings.append(outer)

    def walk(geom: object) -> None:
        if not isinstance(geom, dict):
            raise GeoSourceInvalidError("basemap geometry is malformed")
        kind = geom.get("type")
        if kind == "Polygon":
            take(geom.get("coordinates"))
        elif kind == "MultiPolygon":
            for poly in geom.get("coordinates") or []:
                take(poly)
        elif kind == "GeometryCollection":
            for g in geom.get("geometries") or []:
                walk(g)
        elif kind == "Feature":
            walk(geom.get("geometry"))
        elif kind == "FeatureCollection":
            for f in geom.get("features") or []:
                walk(f)
        else:
            raise GeoSourceInvalidError("basemap has an unsupported geometry type")

    walk(doc)
    return BaseMap(rings=tuple(rings), sha256=sha256, caption=BASEMAP_CAPTION)


def spec_scene_ids(data: GeoSourceData) -> Sequence[str]:
    return [s.script_scene_id for s in data.specs.specs]


__all__ = [
    "BASEMAP_CAPTION",
    "GeoSource",
    "GeoSourceData",
    "LocalDirectoryGeoSource",
    "parse_basemap",
    "spec_scene_ids",
]
