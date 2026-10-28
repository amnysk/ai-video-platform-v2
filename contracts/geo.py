"""地図・地理の契約（ADR-0071）。**GeoManifest / GeoPlace / GeoVerdict の唯一の宣言元**。

地図は生成 AI に想像で描かせず、この manifest（構造化された地点・範囲・ラベル）から描画する
（設計書 v3 §3.2）。manifest が**本当に実在の位置を表しているか**は ``domain/geo`` の純関数が
決定論的に検査し、結果を ``GeoVerification``（``GeoVerdict`` と check の列）にする。

守るもの:

- 座標は **名前付きの** ``lat`` / ``lon``（EPSG:4326）で持ち、範囲を検証する（INV-95）。
  ``(lon, lat)`` / ``(lat, lon)`` の位置引数の tuple・``coordinates`` のような順序が文脈頼みの
  形を受け取らない（``extra=forbid``）。
- 実在の位置を主張する点（``schematic`` 以外）は claim（``EvidenceClaim.claim_id``）と資料
  （``source_ids``）に紐づく。紐づけが実際に解決するか・支持されているかは ``domain/geo`` が見る。
- ``GeoVerification.verdict`` は check の最悪値と**一致しなければ作れない**。BLOCK を含む記録を
  PASS として保存できない（INV-94 の契約側）。LLM の採点で上書きする入口は無い
  （``domain.geo.combine_verdicts`` は等しいか厳しい側にしか動かない）。
- 模式図（``schematic``）に必要な「縮尺不同」の表示が無いことは**構築時ではなく検証器の BLOCK**で
  止める（INV-96）。作れなくするのではなく、BLOCK として記録に残す設計。

この契約はどの workflow・worker にも接続されていない（ADR-0071。接続は後続の枝）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from contracts.artifact_refs import SCRIPT_SCENE_ID_PATTERN, SHA256_HEX_PATTERN, FrozenModel
from contracts.research_evidence import CLAIM_ID_PATTERN, SOURCE_ID_PATTERN

GEO_SCHEMA_VERSION = "1.0"

PLACE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$"
YEAR_MIN = -3000
YEAR_MAX = 3000
MAX_PLACES = 200
MAX_LABELS = 400
MAX_ASSERTIONS = 50
MAX_CHECKS = 2000

Latitude = Annotated[float, Field(ge=-90.0, le=90.0, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0, allow_inf_nan=False)]
Year = Annotated[int, Field(ge=YEAR_MIN, le=YEAR_MAX)]
Crs = Literal["EPSG:4326"]

#: 模式図の表示文に含まれていなければならない語（小文字化して部分一致）。INV-96。
SCHEMATIC_DISCLAIMER_MARKERS: tuple[str, ...] = ("not to scale", "縮尺")


class GeoVerdict(StrEnum):
    """地図の検証の結論。厳しさは PASS < WARN < REVIEW < BLOCK（設計書 v3 §3.3）。"""

    PASS = "pass"
    WARN = "warn"
    REVIEW = "review"
    BLOCK = "block"


GEO_VERDICT_ORDER: tuple[GeoVerdict, ...] = (
    GeoVerdict.PASS,
    GeoVerdict.WARN,
    GeoVerdict.REVIEW,
    GeoVerdict.BLOCK,
)


def stricter(a: GeoVerdict, b: GeoVerdict) -> GeoVerdict:
    """厳しい方。**厳しさの順序の唯一の定義**（``GEO_VERDICT_ORDER``）。"""
    return a if GEO_VERDICT_ORDER.index(a) >= GEO_VERDICT_ORDER.index(b) else b


class MapKind(StrEnum):
    HISTORICAL = "historical"
    MODERN_CONTEXT = "modern_context"
    SCHEMATIC = "schematic"


class LocationConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class GeoFeatureType(StrEnum):
    """地物の種類。距離の許容は種類で変わる（一律の km 閾値を使わない）。"""

    STRUCTURE = "structure"  # 城・寺・建物
    SETTLEMENT = "settlement"  # 都市・町・宿場
    BATTLEFIELD = "battlefield"
    NATURAL = "natural"  # 山・岬・島
    REGION = "region"  # 国・地方（代表点が重心）


class BorderBasis(StrEnum):
    """地図が国境・領域をどの時代の資料で描いているか。"""

    NONE = "none"  # 国境を描かない
    MODERN = "modern"  # 現代の国境
    HISTORICAL = "historical"  # 当時の資料に基づく（``border_valid_year`` を持つ）


class GeoBBox(FrozenModel):
    """想定地域。軸の取り違えを避けるため名前付き（INV-95）。"""

    min_lon: Longitude
    min_lat: Latitude
    max_lon: Longitude
    max_lat: Latitude

    @model_validator(mode="after")
    def _ordered(self) -> GeoBBox:
        if self.min_lon >= self.max_lon or self.min_lat >= self.max_lat:
            raise ValueError("bbox must satisfy min_lon < max_lon and min_lat < max_lat")
        return self

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon


class GeoPlace(FrozenModel):
    place_id: str = Field(pattern=PLACE_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    lat: Latitude
    lon: Longitude
    crs: Crs
    feature_type: GeoFeatureType
    location_confidence: LocationConfidence
    #: 資料自体の位置の不確かさ（km）。``None`` は不明（許容は広げない）
    uncertainty_radius_km: float | None = Field(
        default=None, ge=0.0, le=20000.0, allow_inf_nan=False
    )
    #: この地物が存在した期間（西暦。紀元前は負）。``None`` は不明
    valid_start_year: Year | None = None
    valid_end_year: Year | None = None
    source_ids: tuple[Annotated[str, Field(pattern=SOURCE_ID_PATTERN)], ...] = ()
    #: 位置の主張（``EvidenceClaim.claim_id``）。``schematic`` 以外では必須（manifest が検査）
    claim_id: str | None = Field(default=None, pattern=CLAIM_ID_PATTERN)

    @field_validator("display_name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name must not be blank")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> GeoPlace:
        if (
            self.valid_start_year is not None
            and self.valid_end_year is not None
            and self.valid_start_year > self.valid_end_year
        ):
            raise ValueError("valid_start_year must not be after valid_end_year")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate source_id")
        return self


class GeoLabel(FrozenModel):
    """地図に描く文字。``place_id`` が指す地点の名前と一致していなければ検証器が止める。"""

    place_id: str = Field(pattern=PLACE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label text must not be blank")
        return value


class GeoManifest(FrozenModel):
    scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    map_kind: MapKind
    #: 地図の時間基準（``"1600"`` ``"1600-10"`` ``"-0200"``）。日付を捏造しない。解釈できなければ
    #: 検証器が REVIEW にする
    as_of: str = Field(max_length=32)
    bbox: GeoBBox
    places: Annotated[tuple[GeoPlace, ...], Field(max_length=MAX_PLACES)] = ()
    labels: Annotated[tuple[GeoLabel, ...], Field(max_length=MAX_LABELS)] = ()
    #: 「A は B の東」「経路が X を横切る」など。国境に触れる文は ``borders`` と突き合わせる
    spatial_assertions: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=300)], ...],
        Field(max_length=MAX_ASSERTIONS),
    ] = ()
    borders: BorderBasis = BorderBasis.NONE
    border_valid_year: Year | None = None
    #: ``schematic`` で必須（欠けていれば検証器が BLOCK。INV-96）
    schematic_disclaimer: str | None = Field(default=None, max_length=200)
    #: 描画結果の画像の sha256（描画後に結びつける。任意）
    render_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)

    @model_validator(mode="after")
    def _consistent(self) -> GeoManifest:
        ids = [p.place_id for p in self.places]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate place_id")
        if (self.borders is BorderBasis.HISTORICAL) != (self.border_valid_year is not None):
            raise ValueError("border_valid_year is required iff borders == historical")
        if self.map_kind is not MapKind.SCHEMATIC:
            for place in self.places:
                if place.claim_id is None or not place.source_ids:
                    raise ValueError(
                        f"{place.place_id}: a real-location place needs claim_id and source_ids"
                    )
        return self


# ------------------------------------------------------------------ 参照データ（gazetteer）


class ReferenceName(FrozenModel):
    """参照地点の名前と、その名前が使われた期間（旧称・現称）。"""

    name: str = Field(min_length=1, max_length=200)
    start_year: Year | None = None
    end_year: Year | None = None


class ReferencePlace(FrozenModel):
    """gazetteer 側の 1 地点。検証器へ**注入**される参照（検証器はネットワークに出ない）。"""

    ref_id: str = Field(pattern=PLACE_ID_PATTERN)
    names: Annotated[tuple[ReferenceName, ...], Field(min_length=1, max_length=50)]
    lat: Latitude
    lon: Longitude
    feature_type: GeoFeatureType
    uncertainty_radius_km: float = Field(default=0.0, ge=0.0, le=20000.0, allow_inf_nan=False)
    valid_start_year: Year | None = None
    valid_end_year: Year | None = None


# ------------------------------------------------------------------ 検証結果


class GeoCheck(FrozenModel):
    name: str = Field(min_length=1, max_length=200)
    outcome: GeoVerdict
    detail: str = Field(max_length=500)


class GeoVerification(FrozenModel):
    """検証の結果。``verdict`` は check の最悪値と一致しなければ作れない（INV-94）。"""

    verdict: GeoVerdict
    checks: Annotated[tuple[GeoCheck, ...], Field(min_length=1, max_length=MAX_CHECKS)]

    @model_validator(mode="after")
    def _verdict_is_the_worst_check(self) -> GeoVerification:
        worst = GeoVerdict.PASS
        for check in self.checks:
            worst = stricter(worst, check.outcome)
        if self.verdict is not worst:
            raise ValueError(
                f"verdict {self.verdict.value} does not match the worst check ({worst.value})"
            )
        return self


__all__ = [
    "GEO_SCHEMA_VERSION",
    "GEO_VERDICT_ORDER",
    "PLACE_ID_PATTERN",
    "SCHEMATIC_DISCLAIMER_MARKERS",
    "BorderBasis",
    "GeoBBox",
    "GeoCheck",
    "GeoFeatureType",
    "GeoLabel",
    "GeoManifest",
    "GeoPlace",
    "GeoVerdict",
    "GeoVerification",
    "LocationConfidence",
    "MapKind",
    "ReferenceName",
    "ReferencePlace",
    "stricter",
]
