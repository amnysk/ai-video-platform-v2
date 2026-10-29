"""地図シーンの入力・記録の契約（ADR-0084）。**GeoMapSpec / GeoMapRecord の唯一の宣言元**。

``contracts/geo.py``（GeoManifest と検証）と ``contracts/geo_map.py``（判定結果）の上に載る、
Episode に地図を結線するための契約だけを置く。

- ``GeoMapRequest``: 地図を描く**依頼**。座標を持たない（地点は gazetteer の ``ref_id`` で指す）。
  座標は ``domain.geo.manifest_builder`` が gazetteer から埋めるので、依頼の作者（LLM / 人）が
  座標を書き間違える経路が無い（INV-139）。
- ``GeoMapSpec``: 依頼か、完成済みの manifest のどちらか**一方**。manifest を直接受ける経路は
  LLM が書いた manifest の位置ずれを検証器で止めるためにある。
- ``GeoMapRecord``: 検証・描画・符号化・完成動画の各 sha256 を 1 つに束ねた証跡（INV-141）。

どの workflow の入力 dataclass にも載らない（Temporal 履歴に影響しない。ADR-0084 §7）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from contracts.artifact_refs import (
    SCRIPT_SCENE_ID_PATTERN,
    SHA256_HEX_PATTERN,
    STORYBOARD_SCENE_ID_PATTERN,
    FrozenModel,
)
from contracts.geo import (
    MAX_ASSERTIONS,
    MAX_PLACES,
    PLACE_ID_PATTERN,
    BorderBasis,
    GeoBBox,
    GeoManifest,
    GeoVerdict,
    LocationConfidence,
    MapKind,
    ReferencePlace,
    Year,
    stricter,
)
from contracts.geo_map import MapAccuracyResult, MapPresentationResult
from contracts.research_evidence import CLAIM_ID_PATTERN, SOURCE_ID_PATTERN

GEO_SCENE_SCHEMA_VERSION = "1.0"
MAX_SPECS_PER_EPISODE = 20
#: 地図 1 枚あたりのクリップ尺の上限（ms）。シーン動画の尺が決めるので契約でも有界にする。
CLIP_DURATION_MAX_MS = 120_000


class GeoPlaceRequest(FrozenModel):
    """依頼 1 地点。座標・地物の種類・存在期間は gazetteer が決める（依頼には書けない）。"""

    #: gazetteer の ``ReferencePlace.ref_id``。manifest の ``place_id`` になる
    ref_id: str = Field(pattern=PLACE_ID_PATTERN)
    #: 地図に使う名前。gazetteer のその地点の名前のうち、``as_of`` の時点で使われていたもの
    #: でなければ検証器が止める（旧称・現称）
    display_name: str = Field(min_length=1, max_length=200)
    #: 位置の主張（``EvidenceClaim.claim_id``）と資料。空なら構築時に BLOCK（根拠なしは描かない）
    claim_id: str | None = Field(default=None, pattern=CLAIM_ID_PATTERN)
    source_ids: tuple[Annotated[str, Field(pattern=SOURCE_ID_PATTERN)], ...] = ()
    location_confidence: LocationConfidence = LocationConfidence.MEDIUM
    #: 地図に描く文字。無ければ ``display_name``
    label: str | None = Field(default=None, min_length=1, max_length=200)


class GeoMapRequest(FrozenModel):
    scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    map_kind: MapKind
    as_of: str = Field(max_length=32)
    bbox: GeoBBox
    places: Annotated[tuple[GeoPlaceRequest, ...], Field(max_length=MAX_PLACES)] = ()
    spatial_assertions: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=300)], ...],
        Field(max_length=MAX_ASSERTIONS),
    ] = ()
    borders: BorderBasis = BorderBasis.NONE
    border_valid_year: Year | None = None
    schematic_disclaimer: str | None = Field(default=None, max_length=200)


class GeoMapSpec(FrozenModel):
    """Episode の 1 地図。``request`` か ``manifest`` のどちらか一方。"""

    #: 置き換える storyboard のシーン（``sb<N>``）。台本のシーンは manifest の ``scene_id``
    storyboard_scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    request: GeoMapRequest | None = None
    manifest: GeoManifest | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GeoMapSpec:
        if (self.request is None) == (self.manifest is None):
            raise ValueError("exactly one of request and manifest is required")
        return self

    @property
    def script_scene_id(self) -> str:
        source = self.request if self.request is not None else self.manifest
        assert source is not None
        return source.scene_id


class GeoMapSpecs(FrozenModel):
    """Episode 1 件分の地図の指定（供給元のファイル 1 つ）。"""

    schema_version: Literal["1.0"]
    specs: Annotated[tuple[GeoMapSpec, ...], Field(max_length=MAX_SPECS_PER_EPISODE)]

    @model_validator(mode="after")
    def _unique(self) -> GeoMapSpecs:
        sb = [s.storyboard_scene_id for s in self.specs]
        if len(set(sb)) != len(sb):
            raise ValueError("a storyboard scene can have at most one map")
        return self


class GazetteerFile(FrozenModel):
    """オーナーが供給する gazetteer。``license`` と ``curated_by`` が空の供給は読まない。"""

    schema_version: Literal["1.0"]
    license: str = Field(min_length=1, max_length=200)
    curated_by: str = Field(min_length=1, max_length=200)
    places: tuple[ReferencePlace, ...] = Field(min_length=1, max_length=5000)


class GeoMapRecord(FrozenModel):
    """1 つの地図の証跡。検証・描画・符号化・完成動画の sha256 を束ねる（INV-141）。

    ``verdict`` は ``accuracy`` と ``presentation`` の厳しい側と一致しなければ作れない。
    ``presentation`` が無い（正確さが PASS でない・描かなかった）記録は PASS になれない。
    """

    schema_version: Literal["1.0"]
    scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    storyboard_scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    verdict: GeoVerdict
    #: 検証した manifest（依頼から作れなかったときは ``None``）
    manifest: GeoManifest | None = None
    manifest_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    #: 判定の入力の指紋（gazetteer・根拠）。入力が変われば判定を使い回さない
    gazetteer_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    evidence_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    basemap_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    accuracy: MapAccuracyResult
    presentation: MapPresentationResult | None = None
    #: ``accuracy`` + ``presentation`` の正準 JSON の sha256
    verification_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    png_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    #: 描画の入力（manifest・spec・フォント）の指紋
    render_identity_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    font_id: str | None = Field(default=None, max_length=100)
    render_profile_id: str | None = Field(default=None, max_length=100)
    clip_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    clip_duration_ms: int | None = Field(default=None, gt=0, le=CLIP_DURATION_MAX_MS)
    #: Render が書く。この地図が入った完成動画と描画入力
    final_video_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    input_hash: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)

    @model_validator(mode="after")
    def _consistent(self) -> GeoMapRecord:
        if self.accuracy.scene_id != self.scene_id:
            raise ValueError("accuracy describes another scene")
        if self.presentation is not None and self.presentation.scene_id != self.scene_id:
            raise ValueError("presentation describes another scene")
        expected = (
            self.accuracy.verdict
            if self.presentation is None
            else stricter(self.accuracy.verdict, self.presentation.verdict)
        )
        if self.verdict is not expected:
            raise ValueError(f"verdict {self.verdict.value} must be {expected.value}")
        if self.verdict is GeoVerdict.PASS and (
            self.presentation is None or self.manifest is None or self.png_sha256 is None
        ):
            raise ValueError("a PASS record needs the manifest, the presentation and the image")
        if self.presentation is not None and self.png_sha256 is None:
            raise ValueError("a presentation result belongs to a rendered image")
        if (self.clip_sha256 is None) != (self.clip_duration_ms is None):
            raise ValueError("clip_sha256 and clip_duration_ms go together")
        if self.clip_sha256 is not None and self.png_sha256 is None:
            raise ValueError("a clip is made from a rendered image")
        if (self.final_video_sha256 is None) != (self.input_hash is None):
            raise ValueError("final_video_sha256 and input_hash go together")
        return self


#: 証跡の object key に使う種別名（ArtifactType ではない。行は作らない。BGM の証跡と同じ流儀）
GEO_EVIDENCE_OBJECT_TYPE = "geo_map_evidence"


class GeoRenderEvidence(FrozenModel):
    """Render が完成動画ごとに 1 つ保存する地図の証跡（ADR-0084 §5）。

    ``GEO_MAPS_ENABLED`` が ON の Render は、地図のない Episode でも必ず書く（``records`` が空で
    ``intended_scene_ids`` も空 =「地図を使っていない」の明示）。証跡が無い完成動画は、ON の環境では
    「検査していない」であって「地図なし」ではない。
    """

    schema_version: Literal["1.0"]
    episode_id: str
    final_video_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    input_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    #: 地図を使うと指定された台本シーン（指定ファイル由来）
    intended_scene_ids: tuple[Annotated[str, Field(pattern=SCRIPT_SCENE_ID_PATTERN)], ...] = ()
    records: Annotated[tuple[GeoMapRecord, ...], Field(max_length=MAX_SPECS_PER_EPISODE)] = ()

    @model_validator(mode="after")
    def _consistent(self) -> GeoRenderEvidence:
        if sorted(self.intended_scene_ids) != sorted(r.scene_id for r in self.records):
            raise ValueError("intended scenes and records must describe the same scenes")
        for r in self.records:
            if (r.final_video_sha256, r.input_hash) != (self.final_video_sha256, self.input_hash):
                raise ValueError("every record must be bound to this final video and input")
        return self


__all__ = [
    "GEO_EVIDENCE_OBJECT_TYPE",
    "CLIP_DURATION_MAX_MS",
    "GeoRenderEvidence",
    "GEO_SCENE_SCHEMA_VERSION",
    "MAX_SPECS_PER_EPISODE",
    "GazetteerFile",
    "GeoMapRecord",
    "GeoMapRequest",
    "GeoMapSpec",
    "GeoMapSpecs",
    "GeoPlaceRequest",
]
