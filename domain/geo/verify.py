"""GeoManifest の決定論的検証（ADR-0071）。I/O・時計・乱数なし。同じ入力から同じ結果。

参照データ（gazetteer）と主張（claim）は呼び出し側が**注入**する。ここはネットワークに出ない。
自動で 1 つに決めない: 同名の参照が複数・参照が無い・時間基準が不明は REVIEW。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from contracts.geo import (
    SCHEMATIC_DISCLAIMER_MARKERS,
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
from contracts.research import normalize_claim_text
from contracts.research_evidence import ClaimAssessment, EvidenceClaim
from domain.geo.tolerance import (
    BLOCK_FACTOR,
    classify_distance,
    haversine_km,
    tolerance_km,
)

#: 国境・領域に触れる文を見つける語（小文字化して部分一致）。
BORDER_KEYWORDS: tuple[str, ...] = (
    "border",
    "boundary",
    "frontier",
    "国境",
    "境界",
    "領土",
    "領有",
)
#: 当時の国境の資料の年と地図の時間基準の許容差（年）。
BORDER_YEAR_TOLERANCE = 25
#: 根拠として解決する claim の評価。
RESOLVING_ASSESSMENTS: frozenset[ClaimAssessment] = frozenset(
    {ClaimAssessment.SUPPORTED, ClaimAssessment.QUALIFIED}
)

_AS_OF = re.compile(r"^(-?\d{1,4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def parse_as_of_year(as_of: str) -> int | None:
    """``"1600"`` ``"1600-10"`` ``"-0200"`` などから西暦年。解釈できなければ ``None``。"""
    match = _AS_OF.match(as_of.strip())
    if match is None:
        return None
    month, day = match.group(2), match.group(3)
    if month is not None and not 1 <= int(month) <= 12:
        return None
    if day is not None and not 1 <= int(day) <= 31:
        return None
    return int(match.group(1))


def _check(name: str, outcome: GeoVerdict, detail: str) -> GeoCheck:
    return GeoCheck(name=name, outcome=outcome, detail=detail[:500])


def _finish(checks: list[GeoCheck]) -> GeoVerification:
    worst = GeoVerdict.PASS
    for c in checks:
        worst = stricter(worst, c.outcome)
    return GeoVerification(verdict=worst, checks=tuple(checks))


def _resolve(place: GeoPlace, references: Sequence[ReferencePlace]) -> list[ReferencePlace]:
    """参照候補。place_id が参照の id と一致すれば、それが明示された 1 件。なければ名前で照合。"""
    by_id = [r for r in references if r.ref_id == place.place_id]
    if len(by_id) == 1:
        return by_id
    wanted = normalize_claim_text(place.display_name)
    return [r for r in references if any(normalize_claim_text(n.name) == wanted for n in r.names)]


def _in_period(year: int, start: int | None, end: int | None) -> bool:
    return (start is None or year >= start) and (end is None or year <= end)


# ------------------------------------------------------------------ manifest 全体


def verify_manifest(
    manifest: GeoManifest,
    reference_places: Sequence[ReferencePlace],
    evidence_claims: Sequence[EvidenceClaim],
) -> GeoVerification:
    checks: list[GeoCheck] = []
    schematic = manifest.map_kind is MapKind.SCHEMATIC
    year: int | None = None

    if schematic:
        checks.append(_schematic_disclaimer(manifest))
    else:
        year = parse_as_of_year(manifest.as_of)
        if year is None:
            checks.append(
                _check("as_of", GeoVerdict.REVIEW, f"time basis {manifest.as_of!r} is unknown")
            )
        else:
            checks.append(_check("as_of", GeoVerdict.PASS, f"year {year}"))
        checks.append(_borders(manifest, year))
        if not manifest.places:
            checks.append(_check("places", GeoVerdict.REVIEW, "a real map has no places to verify"))

    claims = {c.claim_id: c for c in evidence_claims}
    resolved: dict[str, list[ReferencePlace]] = {}
    for place in manifest.places:
        if schematic:
            continue  # 模式図は実在の位置を主張しない
        candidates = _resolve(place, reference_places)
        resolved[place.place_id] = candidates
        checks.extend(_place_checks(manifest, place, candidates, year, claims))

    checks.extend(_label_checks(manifest, resolved))
    return _finish(checks)


def verify_scene(
    claims_real_location: bool,
    manifest: GeoManifest | None,
    reference_places: Sequence[ReferencePlace],
    evidence_claims: Sequence[EvidenceClaim],
) -> GeoVerification:
    """INV-93: 実在の位置を主張するシーンは manifest を要する（無ければ BLOCK）。"""
    if manifest is None:
        if claims_real_location:
            return _finish(
                [_check("manifest_required", GeoVerdict.BLOCK, "real-location map has no manifest")]
            )
        return _finish([_check("no_real_location_claim", GeoVerdict.PASS, "no map to verify")])
    result = verify_manifest(manifest, reference_places, evidence_claims)
    if claims_real_location and manifest.map_kind is MapKind.SCHEMATIC:
        extra = _check(
            "schematic_claims_real_location",
            GeoVerdict.BLOCK,
            "a schematic map cannot be presented as a real location",
        )
        return _finish([*result.checks, extra])
    return result


def _schematic_disclaimer(manifest: GeoManifest) -> GeoCheck:
    text = (manifest.schematic_disclaimer or "").strip()
    folded = normalize_claim_text(text)
    if text and any(marker in folded for marker in SCHEMATIC_DISCLAIMER_MARKERS):
        return _check("schematic_disclaimer", GeoVerdict.PASS, "disclaimer present")
    return _check(
        "schematic_disclaimer",
        GeoVerdict.BLOCK,
        "a schematic map needs a visible 'Not to scale' disclaimer",
    )


def _borders(manifest: GeoManifest, year: int | None) -> GeoCheck:
    kind = manifest.map_kind
    if kind is MapKind.HISTORICAL and manifest.borders is BorderBasis.MODERN:
        return _check(
            "borders",
            GeoVerdict.REVIEW,
            "modern borders on a historical map without historical data",
        )
    border_year = manifest.border_valid_year
    if (
        manifest.borders is BorderBasis.HISTORICAL
        and border_year is not None
        and year is not None
        and abs(border_year - year) > BORDER_YEAR_TOLERANCE
    ):
        return _check(
            "borders",
            GeoVerdict.REVIEW,
            f"border data of {border_year} does not match as_of {year}",
        )
    if manifest.borders is BorderBasis.NONE:
        for text in manifest.spatial_assertions:
            folded = normalize_claim_text(text)
            if any(word in folded for word in BORDER_KEYWORDS):
                return _check(
                    "borders", GeoVerdict.REVIEW, "a border assertion has no border data basis"
                )
    return _check("borders", GeoVerdict.PASS, f"borders: {manifest.borders.value}")


# ------------------------------------------------------------------ 地点


def _place_checks(
    manifest: GeoManifest,
    place: GeoPlace,
    candidates: list[ReferencePlace],
    year: int | None,
    claims: dict[str, EvidenceClaim],
) -> list[GeoCheck]:
    pid = place.place_id
    out: list[GeoCheck] = [_bounds(manifest, place)]
    ref = candidates[0] if len(candidates) == 1 else None

    if not candidates:
        out.append(
            _check(f"{pid}.reference", GeoVerdict.REVIEW, "no reference place; unverifiable")
        )
    elif ref is None:
        ids = ", ".join(r.ref_id for r in candidates)
        out.append(
            _check(
                f"{pid}.reference",
                GeoVerdict.REVIEW,
                f"homonym: {len(candidates)} reference candidates ({ids}); not auto-picked",
            )
        )
    else:
        out.append(_check(f"{pid}.reference", GeoVerdict.PASS, f"resolved to {ref.ref_id}"))

    out.append(_swap(manifest, place, ref))
    if ref is not None:
        out.append(_distance(place, ref))
    out.extend(_valid_time(manifest, place, ref, year))
    if ref is not None:
        out.extend(_name_era(manifest, place, ref, year))
    out.append(_claim(place, claims))
    return out


def _bounds(manifest: GeoManifest, place: GeoPlace) -> GeoCheck:
    name = f"{place.place_id}.bounds"
    if (
        not (-90.0 <= place.lat <= 90.0 and -180.0 <= place.lon <= 180.0)
        or place.crs != "EPSG:4326"
    ):
        return _check(name, GeoVerdict.BLOCK, "coordinates are outside EPSG:4326 ranges")
    if not manifest.bbox.contains(place.lat, place.lon):
        return _check(name, GeoVerdict.REVIEW, "point is outside the expected region bbox")
    return _check(name, GeoVerdict.PASS, "inside the expected region")


def _ref_tolerance(place: GeoPlace, ref: ReferencePlace) -> float:
    return tolerance_km(
        place.feature_type,
        place.location_confidence,
        place.uncertainty_radius_km,
        ref.uncertainty_radius_km,
    )


def _swap(manifest: GeoManifest, place: GeoPlace, ref: ReferencePlace | None) -> GeoCheck:
    """(lat, lon) を入れ替えた点が想定地域・参照位置に合うなら、軸順の取り違え。"""
    name = f"{place.place_id}.swap"
    if abs(place.lon) > 90.0:  # 入れ替えると緯度の範囲外。契約（INV-95）が先に弾く形
        return _check(name, GeoVerdict.PASS, "swap is not representable")
    sw_lat, sw_lon = place.lon, place.lat
    if manifest.bbox.contains(sw_lat, sw_lon) and not manifest.bbox.contains(place.lat, place.lon):
        return _check(name, GeoVerdict.BLOCK, "lat/lon look swapped (fits the region only swapped)")
    if ref is not None:
        tol = _ref_tolerance(place, ref)
        drawn = haversine_km(place.lat, place.lon, ref.lat, ref.lon)
        swapped = haversine_km(sw_lat, sw_lon, ref.lat, ref.lon)
        if drawn > tol * BLOCK_FACTOR and swapped <= tol:
            return _check(
                name, GeoVerdict.BLOCK, "lat/lon look swapped (swapped point hits reference)"
            )
    return _check(name, GeoVerdict.PASS, "no sign of swapped axes")


def _distance(place: GeoPlace, ref: ReferencePlace) -> GeoCheck:
    tol = _ref_tolerance(place, ref)
    dist = haversine_km(place.lat, place.lon, ref.lat, ref.lon)
    outcome = classify_distance(dist, tol)
    return _check(
        f"{place.place_id}.distance",
        outcome,
        f"{dist:.1f} km from {ref.ref_id}; tolerance {tol:.1f} km "
        f"({place.feature_type.value}, {place.location_confidence.value})",
    )


def _valid_time(
    manifest: GeoManifest, place: GeoPlace, ref: ReferencePlace | None, year: int | None
) -> list[GeoCheck]:
    if year is None:
        return []
    name = f"{place.place_id}.valid_time"
    out: list[GeoCheck] = []
    if not _in_period(year, place.valid_start_year, place.valid_end_year):
        outcome = GeoVerdict.BLOCK if manifest.map_kind is MapKind.HISTORICAL else GeoVerdict.REVIEW
        out.append(_check(name, outcome, f"{year} is outside the place's valid period"))
    elif ref is not None and not _in_period(year, ref.valid_start_year, ref.valid_end_year):
        out.append(
            _check(name, GeoVerdict.REVIEW, f"{year} is outside the reference's valid period")
        )
    else:
        out.append(_check(name, GeoVerdict.PASS, f"valid in {year}"))
    return out


def _name_era(
    manifest: GeoManifest, place: GeoPlace, ref: ReferencePlace, year: int | None
) -> list[GeoCheck]:
    name = f"{place.place_id}.name_era"
    wanted = normalize_claim_text(place.display_name)
    matching = [n for n in ref.names if normalize_claim_text(n.name) == wanted]
    if not matching:
        return [_check(name, GeoVerdict.WARN, "display_name is not a known name of the reference")]
    if year is None or manifest.map_kind is not MapKind.HISTORICAL:
        return [_check(name, GeoVerdict.PASS, "name period not applicable")]
    if any(_in_period(year, n.start_year, n.end_year) for n in matching):
        return [_check(name, GeoVerdict.PASS, f"name was in use in {year}")]
    return [_check(name, GeoVerdict.REVIEW, f"name was not in use in {year} (anachronistic)")]


def _claim(place: GeoPlace, claims: dict[str, EvidenceClaim]) -> GeoCheck:
    name = f"{place.place_id}.claim"
    claim = claims.get(place.claim_id) if place.claim_id is not None else None
    if claim is None:
        return _check(name, GeoVerdict.REVIEW, f"claim {place.claim_id} does not resolve")
    if claim.assessment not in RESOLVING_ASSESSMENTS:
        return _check(
            name, GeoVerdict.REVIEW, f"claim {claim.claim_id} is {claim.assessment.value}"
        )
    return _check(name, GeoVerdict.PASS, f"claim {claim.claim_id} is {claim.assessment.value}")


# ------------------------------------------------------------------ ラベル


def _label_checks(
    manifest: GeoManifest, resolved: dict[str, list[ReferencePlace]]
) -> list[GeoCheck]:
    places = {p.place_id: p for p in manifest.places}
    out: list[GeoCheck] = []
    for label in manifest.labels:
        name = f"label[{label.place_id}]"
        place = places.get(label.place_id)
        if place is None:
            out.append(_check(name, GeoVerdict.BLOCK, "label points to an unknown place_id"))
            continue
        accepted = {normalize_claim_text(place.display_name)}
        candidates = resolved.get(place.place_id, [])
        if len(candidates) == 1:
            accepted |= {normalize_claim_text(n.name) for n in candidates[0].names}
        if normalize_claim_text(label.text) in accepted:
            out.append(_check(name, GeoVerdict.PASS, "label matches the place name"))
        else:
            out.append(
                _check(
                    name,
                    GeoVerdict.BLOCK,
                    f"label {label.text!r} does not match {place.display_name!r}",
                )
            )
    return out
