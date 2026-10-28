"""GeoManifest の決定論的検証（ADR-0071）。I/O・時計・乱数なし。同じ入力から同じ結果。

参照データ（gazetteer）と主張（claim）は呼び出し側が**注入**する。ここはネットワークに出ない。
自動で 1 つに決めない: 同名の参照が複数・参照が無い・時間基準が不明は REVIEW。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from contracts.geo import (
    YEAR_MAX,
    YEAR_MIN,
    BorderBasis,
    GeoCheck,
    GeoManifest,
    GeoPlace,
    GeoVerdict,
    GeoVerification,
    MapKind,
    ReferencePlace,
    schematic_disclaimer_ok,
    stricter,
)
from contracts.research import ClaimKind, normalize_claim_text
from contracts.research_evidence import (
    ClaimAssessment,
    EvidenceClaim,
    EvidenceSource,
    SourceFetchStatus,
)
from domain.geo.tolerance import (
    BASE_TOLERANCE_KM,
    BLOCK_FACTOR,
    PLACE_UNCERTAINTY_CAP_FACTOR,
    classify_distance,
    haversine_km,
    tolerance_km,
)

#: 国境・領域に触れる文を見つける語（小文字化して部分一致）。
BORDER_KEYWORDS: tuple[str, ...] = (
    "border",
    "boundary",
    "frontier",
    "territory",
    "kingdom",
    "empire",
    "realm",
    "国境",
    "境界",
    "領土",
    "領有",
    "領地",
    "藩",
    "王国",
    "帝国",
)
#: 当時の国境の資料の年と地図の時間基準の許容差（年）。初期値（おおよそ 1 世代。国境は数十年で
#: 動くが、1 年単位の食い違いは資料の粒度の範囲）。実データで調整する（ADR-0071）。
BORDER_YEAR_TOLERANCE = 25
#: 根拠として解決する claim の評価。
RESOLVING_ASSESSMENTS: frozenset[ClaimAssessment] = frozenset(
    {ClaimAssessment.SUPPORTED, ClaimAssessment.QUALIFIED}
)

#: 年は**ゼロ埋め 4 桁**（``0330`` ``1600`` ``-0200``）。ASCII の数字だけ（全角を受けない）。
#: ``12-05`` のような年が曖昧な書式は解釈しない。
_AS_OF = re.compile(r"^(-?[0-9]{4})(?:-([0-9]{2}))?(?:-([0-9]{2}))?$")
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap(year: int) -> bool:
    """先発グレゴリオ暦（紀元前は天文学的年数: 1 BC = 0 年は使わないので負の年はそのまま）。"""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def parse_as_of_year(as_of: str) -> int | None:
    """``"1600"`` ``"1600-10"`` ``"-0200"`` などから西暦年。解釈できなければ ``None``。

    年 0・契約の範囲（-3000〜3000）外・存在しない日付（``1600-02-31``）・曖昧な書式は ``None``。
    """
    match = _AS_OF.fullmatch(as_of)
    if match is None:
        return None
    year = int(match.group(1))
    if year == 0 or not YEAR_MIN <= year <= YEAR_MAX:
        return None
    month, day = match.group(2), match.group(3)
    if month is not None and not 1 <= int(month) <= 12:
        return None
    if day is not None:
        if month is None:
            return None
        limit = _DAYS_IN_MONTH[int(month) - 1] + (1 if int(month) == 2 and _is_leap(year) else 0)
        if not 1 <= int(day) <= limit:
            return None
    return year


def _check(name: str, outcome: GeoVerdict, detail: str) -> GeoCheck:
    return GeoCheck(name=name, outcome=outcome, detail=detail[:500])


def _finish(checks: list[GeoCheck]) -> GeoVerification:
    worst = GeoVerdict.PASS
    for c in checks:
        worst = stricter(worst, c.outcome)
    return GeoVerification(verdict=worst, checks=tuple(checks))


def resolve_reference_candidates(
    place: GeoPlace, references: Sequence[ReferencePlace]
) -> list[ReferencePlace]:
    """参照候補。place_id が参照の id と一致すれば、それが明示された 1 件。なければ名前で照合。"""
    by_id = [r for r in references if r.ref_id == place.place_id]
    if len(by_id) == 1:
        return by_id
    wanted = normalize_claim_text(place.display_name)
    return [r for r in references if any(normalize_claim_text(n.name) == wanted for n in r.names)]


_ASSESSMENT_RANK: dict[ClaimAssessment, int] = {
    ClaimAssessment.SUPPORTED: 3,
    ClaimAssessment.QUALIFIED: 2,
    ClaimAssessment.DISPUTED: 1,
    ClaimAssessment.INSUFFICIENT: 0,
}


def _index_claims(
    evidence_claims: Sequence[EvidenceClaim],
) -> tuple[dict[str, EvidenceClaim], list[str]]:
    """claim_id で引く。重複は**最も弱い評価**を採る（後勝ちにしない）。評価の違う重複を返す。"""
    index: dict[str, EvidenceClaim] = {}
    conflicting: set[str] = set()
    for c in evidence_claims:
        prev = index.get(c.claim_id)
        if prev is None:
            index[c.claim_id] = c
            continue
        if prev.assessment is not c.assessment or prev.kind is not c.kind:
            conflicting.add(c.claim_id)
        if _ASSESSMENT_RANK[c.assessment] < _ASSESSMENT_RANK[prev.assessment]:
            index[c.claim_id] = c
    return index, sorted(conflicting)


def _in_period(year: int, start: int | None, end: int | None) -> bool:
    return (start is None or year >= start) and (end is None or year <= end)


# ------------------------------------------------------------------ manifest 全体


def verify_manifest(
    manifest: GeoManifest,
    reference_places: Sequence[ReferencePlace],
    evidence_claims: Sequence[EvidenceClaim],
    evidence_sources: Sequence[EvidenceSource] | None = None,
) -> GeoVerification:
    """``evidence_sources`` を渡したときだけ ``source_ids`` の解決を検査する。"""
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

    claims, duplicates = _index_claims(evidence_claims)
    if duplicates:
        checks.append(
            _check(
                "claims.duplicate",
                GeoVerdict.REVIEW,
                f"claim_id appears with different assessments: {', '.join(duplicates)}",
            )
        )
    sources = None if evidence_sources is None else {s.source_id: s for s in evidence_sources}
    resolved: dict[str, list[ReferencePlace]] = {}
    for place in manifest.places:
        if schematic:
            continue  # 模式図は実在の位置を主張しない
        candidates = resolve_reference_candidates(place, reference_places)
        resolved[place.place_id] = candidates
        checks.extend(
            _place_checks(manifest, place, candidates, year, claims, sources, reference_places)
        )

    checks.extend(_label_checks(manifest, resolved, year))
    return _finish(checks)


def verify_scene(
    claims_real_location: bool,
    manifest: GeoManifest | None,
    reference_places: Sequence[ReferencePlace],
    evidence_claims: Sequence[EvidenceClaim],
    evidence_sources: Sequence[EvidenceSource] | None = None,
) -> GeoVerification:
    """INV-93: 実在の位置を主張するシーンは manifest を要する（無ければ BLOCK）。"""
    if manifest is None:
        if claims_real_location:
            return _finish(
                [_check("manifest_required", GeoVerdict.BLOCK, "real-location map has no manifest")]
            )
        return _finish([_check("no_real_location_claim", GeoVerdict.PASS, "no map to verify")])
    result = verify_manifest(manifest, reference_places, evidence_claims, evidence_sources)
    if claims_real_location and manifest.map_kind is MapKind.SCHEMATIC:
        extra = _check(
            "schematic_claims_real_location",
            GeoVerdict.BLOCK,
            "a schematic map cannot be presented as a real location",
        )
        return _finish([*result.checks, extra])
    return result


def _schematic_disclaimer(manifest: GeoManifest) -> GeoCheck:
    if schematic_disclaimer_ok(manifest.schematic_disclaimer):
        return _check("schematic_disclaimer", GeoVerdict.PASS, "disclaimer present")
    return _check(
        "schematic_disclaimer",
        GeoVerdict.BLOCK,
        "a schematic map needs a visible 'Not to scale' disclaimer that does not claim accuracy",
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
    sources: dict[str, EvidenceSource] | None,
    references: Sequence[ReferencePlace],
) -> list[GeoCheck]:
    pid = place.place_id
    out: list[GeoCheck] = [_bounds(manifest, place), _uncertainty(place)]
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
        out.extend(_name(place, ref, references))
    out.extend(_valid_time(manifest, place, ref, year))
    if ref is not None:
        out.extend(_name_era(manifest, place, ref, year))
    out.extend(_claim(place, claims, ref))
    if sources is not None:
        out.append(_sources(place, sources))
    return out


def _uncertainty(place: GeoPlace) -> GeoCheck:
    """自己申告の不確かさが許容へ寄与できる上限を超えたら REVIEW（B1）。"""
    name = f"{place.place_id}.uncertainty"
    cap = BASE_TOLERANCE_KM[place.feature_type] * PLACE_UNCERTAINTY_CAP_FACTOR
    declared = place.uncertainty_radius_km
    if declared is not None and declared > cap:
        return _check(
            name,
            GeoVerdict.REVIEW,
            f"declared uncertainty {declared:g} km exceeds the cap {cap:g} km for "
            f"{place.feature_type.value}; only the cap counts toward the tolerance",
        )
    return _check(name, GeoVerdict.PASS, "declared uncertainty is within the cap")


def _name(
    place: GeoPlace, ref: ReferencePlace, references: Sequence[ReferencePlace]
) -> list[GeoCheck]:
    """作者が選んだ id でも、名前が参照の既知の名前でなければ止める（M2）。"""
    name = f"{place.place_id}.name"
    wanted = normalize_claim_text(place.display_name)
    if any(normalize_claim_text(n.name) == wanted for n in ref.names):
        return [_check(name, GeoVerdict.PASS, "display_name is a known name of the reference")]
    tol = _ref_tolerance(place, ref)
    elsewhere = [
        r
        for r in references
        if r.ref_id != ref.ref_id
        and any(normalize_claim_text(n.name) == wanted for n in r.names)
        and haversine_km(r.lat, r.lon, ref.lat, ref.lon) > tol
    ]
    if elsewhere:
        ids = ", ".join(r.ref_id for r in elsewhere)
        return [
            _check(
                name,
                GeoVerdict.BLOCK,
                f"display_name {place.display_name!r} names another place ({ids}) "
                f"and not {ref.ref_id}",
            )
        ]
    return [
        _check(
            name,
            GeoVerdict.REVIEW,
            f"display_name {place.display_name!r} is not a known name of {ref.ref_id}",
        )
    ]


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
        return []  # 名前が参照に無いことは ``.name`` が止める
    if year is None or manifest.map_kind is not MapKind.HISTORICAL:
        return [_check(name, GeoVerdict.PASS, "name period not applicable")]
    if any(_in_period(year, n.start_year, n.end_year) for n in matching):
        return [_check(name, GeoVerdict.PASS, f"name was in use in {year}")]
    return [_check(name, GeoVerdict.REVIEW, f"name was not in use in {year} (anachronistic)")]


def _claim(
    place: GeoPlace, claims: dict[str, EvidenceClaim], ref: ReferencePlace | None
) -> list[GeoCheck]:
    name = f"{place.place_id}.claim"
    claim = claims.get(place.claim_id) if place.claim_id is not None else None
    if claim is None:
        return [_check(name, GeoVerdict.REVIEW, f"claim {place.claim_id} does not resolve")]
    if claim.assessment not in RESOLVING_ASSESSMENTS:
        return [
            _check(name, GeoVerdict.REVIEW, f"claim {claim.claim_id} is {claim.assessment.value}")
        ]
    if claim.kind is not ClaimKind.PLACE:
        return [
            _check(name, GeoVerdict.REVIEW, f"claim {claim.claim_id} is a {claim.kind.value} claim")
        ]
    out = [_check(name, GeoVerdict.PASS, f"claim {claim.claim_id} is {claim.assessment.value}")]
    names = {normalize_claim_text(place.display_name)}
    if ref is not None:
        names |= {normalize_claim_text(n.name) for n in ref.names}
    text = normalize_claim_text(claim.text)
    related = any(n in text for n in names)
    out.append(
        _check(
            f"{place.place_id}.claim_relevance",
            GeoVerdict.PASS if related else GeoVerdict.WARN,
            "claim text mentions the place"
            if related
            else "claim text does not mention the place name or its aliases",
        )
    )
    return out


def _sources(place: GeoPlace, sources: dict[str, EvidenceSource]) -> GeoCheck:
    name = f"{place.place_id}.sources"
    bad = [
        sid
        for sid in place.source_ids
        if sid not in sources or sources[sid].fetch_status is not SourceFetchStatus.FETCHED
    ]
    if bad or not place.source_ids:
        return _check(
            name,
            GeoVerdict.REVIEW,
            f"sources not resolved to a fetched source: {', '.join(bad) or '(none declared)'}",
        )
    return _check(name, GeoVerdict.PASS, "all sources resolve to fetched sources")


# ------------------------------------------------------------------ ラベル


def _label_checks(
    manifest: GeoManifest, resolved: dict[str, list[ReferencePlace]], year: int | None
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
        folded = normalize_claim_text(label.text)
        if folded in accepted:
            out.append(_label_era(name, manifest, candidates, folded, year))
        else:
            out.append(
                _check(
                    name,
                    GeoVerdict.BLOCK,
                    f"label {label.text!r} does not match {place.display_name!r}",
                )
            )
    return out


def _label_era(
    name: str,
    manifest: GeoManifest,
    candidates: list[ReferencePlace],
    folded: str,
    year: int | None,
) -> GeoCheck:
    """歴史地図では、ラベルの名前もその時代に使われていたものだけ（M1）。"""
    if manifest.map_kind is MapKind.HISTORICAL and year is not None and len(candidates) == 1:
        matching = [n for n in candidates[0].names if normalize_claim_text(n.name) == folded]
        if matching and not any(_in_period(year, n.start_year, n.end_year) for n in matching):
            return _check(name, GeoVerdict.REVIEW, f"label name was not in use in {year}")
    return _check(name, GeoVerdict.PASS, "label matches the place name")
