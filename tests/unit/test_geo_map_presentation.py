"""地図の見た目の品質（ADR-0078、INV-118）。正確さとは別の関数・別の理由コード。

理由は docs/testing/geo-maps-rationale.md。
"""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from contracts.geo import GEO_VERDICT_ORDER, GeoVerdict, stricter
from contracts.geo_map import (
    CombinedMapResult,
    LayoutText,
    MapAccuracyResult,
    MapFinding,
    MapLayout,
    MapPresentationResult,
    MapReasonCode,
    MapTargetKind,
    PixelBox,
    PresentationReasonCode,
    Rgb,
)
from domain.geo.map_presentation import (
    MIN_CONTRAST_RATIO,
    MIN_LABEL_FONT_PX,
    check_map_presentation,
    combine_map_results,
    contrast_ratio,
)

BLACK = Rgb(r=0, g=0, b=0)
WHITE = Rgb(r=255, g=255, b=255)
P = PresentationReasonCode


def _text(
    target: str = "p1",
    box: tuple[int, int, int, int] = (100, 100, 300, 140),
    *,
    font_px: int = 36,
    fg: Rgb = WHITE,
    bg: Rgb = BLACK,
) -> LayoutText:
    x0, y0, x1, y1 = box
    return LayoutText(
        target_id=target,
        box=PixelBox(x0=x0, y0=y0, x1=x1, y1=y1),
        font_px=font_px,
        text_rgb=fg,
        plate_rgb=bg,
    )


def _layout(*labels: LayoutText, **over: object) -> MapLayout:
    base: dict[str, object] = {"scene_id": "s1", "width": 1080, "height": 1920, "labels": labels}
    base.update(over)
    return MapLayout.model_validate(base)


def _codes(result: MapPresentationResult) -> set[tuple[PresentationReasonCode, GeoVerdict]]:
    return {(f.reason_code, f.verdict) for f in result.findings}


def test_a_legible_layout_passes_with_one_ok_finding() -> None:
    result = check_map_presentation(_layout(_text("p1"), _text("p2", (100, 200, 300, 240))))
    assert result.verdict is GeoVerdict.PASS
    assert [f.reason_code for f in result.findings] == [P.OK]


def test_overlapping_labels_are_flagged_for_both_targets() -> None:
    result = check_map_presentation(_layout(_text("p1"), _text("p2", (250, 120, 450, 160))))
    assert (P.LABEL_OVERLAP, GeoVerdict.REVIEW) in _codes(result)
    assert {f.target_id for f in result.findings if f.reason_code is P.LABEL_OVERLAP} == {
        "p1",
        "p2",
    }


def test_touching_edges_are_not_an_overlap() -> None:
    result = check_map_presentation(_layout(_text("p1"), _text("p2", (300, 100, 500, 140))))
    assert result.verdict is GeoVerdict.PASS


def test_a_font_below_the_minimum_is_flagged() -> None:
    result = check_map_presentation(_layout(_text(font_px=MIN_LABEL_FONT_PX - 1)))
    assert (P.FONT_TOO_SMALL, GeoVerdict.REVIEW) in _codes(result)


def test_a_font_exactly_at_the_minimum_is_accepted() -> None:
    assert (
        check_map_presentation(_layout(_text(font_px=MIN_LABEL_FONT_PX))).verdict is GeoVerdict.PASS
    )


def test_low_contrast_is_flagged() -> None:
    grey = Rgb(r=128, g=128, b=128)
    result = check_map_presentation(_layout(_text(fg=grey, bg=Rgb(r=140, g=140, b=140))))
    assert (P.LOW_CONTRAST, GeoVerdict.REVIEW) in _codes(result)


def test_contrast_ratio_matches_the_wcag_definition() -> None:
    assert contrast_ratio(WHITE, BLACK) == pytest.approx(21.0)
    assert contrast_ratio(BLACK, BLACK) == pytest.approx(1.0)
    assert contrast_ratio(WHITE, BLACK) == contrast_ratio(BLACK, WHITE)
    assert MIN_CONTRAST_RATIO == 4.5


@pytest.mark.parametrize(
    "box",
    [(-1, 100, 200, 140), (100, -5, 200, 140), (900, 100, 1081, 140), (100, 1900, 300, 1921)],
)
def test_a_label_outside_the_canvas_is_flagged(box: tuple[int, int, int, int]) -> None:
    result = check_map_presentation(_layout(_text(box=box)))
    assert (P.LABEL_OUTSIDE_CANVAS, GeoVerdict.REVIEW) in _codes(result)


def test_a_label_touching_the_canvas_edge_is_inside() -> None:
    result = check_map_presentation(_layout(_text(box=(0, 0, 200, 40))))
    assert result.verdict is GeoVerdict.PASS


def test_a_required_disclaimer_that_is_missing_is_blocked() -> None:
    result = check_map_presentation(_layout(_text(), disclaimer_required=True))
    assert result.verdict is GeoVerdict.BLOCK
    assert (P.DISCLAIMER_NOT_VISIBLE, GeoVerdict.BLOCK) in _codes(result)


def test_a_disclaimer_off_canvas_or_unreadable_is_blocked() -> None:
    off = _text("s1", (0, 1900, 400, 1990), font_px=36)
    tiny = _text("s1", (0, 1800, 400, 1840), font_px=MIN_LABEL_FONT_PX - 4)
    faint = _text(
        "s1", (0, 1800, 400, 1840), fg=Rgb(r=120, g=120, b=120), bg=Rgb(r=130, g=130, b=130)
    )
    for disclaimer in (off, tiny, faint):
        result = check_map_presentation(
            _layout(_text(), disclaimer=disclaimer, disclaimer_required=True)
        )
        assert result.verdict is GeoVerdict.BLOCK, disclaimer


def test_a_visible_disclaimer_passes() -> None:
    disclaimer = _text("s1", (0, 1800, 600, 1850))
    result = check_map_presentation(_layout(disclaimer=disclaimer, disclaimer_required=True))
    assert result.verdict is GeoVerdict.PASS


def test_presentation_is_deterministic() -> None:
    layout = _layout(_text("p1"), _text("p2", (250, 120, 450, 160)))
    assert check_map_presentation(layout) == check_map_presentation(layout)


def test_presentation_findings_use_presentation_codes_not_accuracy_codes() -> None:
    assert not ({c.value for c in PresentationReasonCode} - {"ok"}) & {
        c.value for c in MapReasonCode
    }


# ------------------------------------------------------------------ 合流（INV-118）


def _accuracy(verdict: GeoVerdict) -> MapAccuracyResult:
    if verdict is GeoVerdict.PASS:
        finding = MapFinding(
            verdict=verdict,
            reason_code=MapReasonCode.OK,
            target_kind=MapTargetKind.MANIFEST,
            target_id="s1",
        )
    else:
        finding = MapFinding(
            verdict=verdict,
            reason_code=MapReasonCode.POSITION_MISMATCH,
            target_kind=MapTargetKind.PLACE,
            target_id="p1",
        )
    return MapAccuracyResult(scene_id="s1", verdict=verdict, findings=(finding,))


def _presentation(verdict: GeoVerdict) -> MapPresentationResult:
    if verdict is GeoVerdict.PASS:
        layout = _layout(_text())
    else:
        layout = _layout(_text(font_px=MIN_LABEL_FONT_PX - 1))
    result = check_map_presentation(layout)
    assert result.verdict in (verdict, GeoVerdict.REVIEW)
    return result


@pytest.mark.parametrize("accuracy", list(GEO_VERDICT_ORDER))
@pytest.mark.parametrize("presentation", [GeoVerdict.PASS, GeoVerdict.REVIEW])
def test_good_presentation_never_upgrades_accuracy(
    accuracy: GeoVerdict, presentation: GeoVerdict
) -> None:
    acc, pres = _accuracy(accuracy), _presentation(presentation)
    combined = combine_map_results(acc, pres)
    assert combined.verdict is stricter(acc.verdict, pres.verdict)
    # 見た目がどうでも、正確さより緩くならない。
    assert stricter(combined.verdict, acc.verdict) is combined.verdict


def test_a_perfectly_legible_inaccurate_map_is_still_blocked() -> None:
    combined = combine_map_results(_accuracy(GeoVerdict.BLOCK), _presentation(GeoVerdict.PASS))
    assert combined.verdict is GeoVerdict.BLOCK


def test_poor_presentation_alone_does_not_block_an_accurate_map() -> None:
    combined = combine_map_results(_accuracy(GeoVerdict.PASS), _presentation(GeoVerdict.REVIEW))
    assert combined.verdict is GeoVerdict.REVIEW


def test_a_combined_result_cannot_be_milder_than_accuracy() -> None:
    with pytest.raises(ValidationError):
        CombinedMapResult(
            scene_id="s1",
            verdict=GeoVerdict.PASS,
            accuracy=_accuracy(GeoVerdict.BLOCK),
            presentation=_presentation(GeoVerdict.PASS),
        )


def test_results_for_different_scenes_do_not_combine() -> None:
    other = check_map_presentation(_layout(_text(), scene_id="s2"))
    with pytest.raises(ValueError):
        combine_map_results(_accuracy(GeoVerdict.PASS), other)


def test_the_combiner_accepts_only_results_never_a_verdict_or_score() -> None:
    with pytest.raises(TypeError):
        combine_map_results(_accuracy(GeoVerdict.BLOCK), GeoVerdict.PASS)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        combine_map_results(_accuracy(GeoVerdict.BLOCK), 0.99)  # type: ignore[arg-type]


def test_every_pair_of_verdicts_is_monotone() -> None:
    for a, p in itertools.product(GEO_VERDICT_ORDER, [GeoVerdict.PASS, GeoVerdict.REVIEW]):
        combined = combine_map_results(_accuracy(a), _presentation(p))
        assert GEO_VERDICT_ORDER.index(combined.verdict) >= GEO_VERDICT_ORDER.index(a)


def test_a_label_covering_the_disclaimer_is_an_overlap() -> None:
    disclaimer = _text("s1", (0, 1800, 600, 1850))
    label = _text("p1", (200, 1820, 400, 1860))
    result = check_map_presentation(_layout(label, disclaimer=disclaimer, disclaimer_required=True))
    assert (P.LABEL_OVERLAP, GeoVerdict.REVIEW) in _codes(result)
    assert {f.target_id for f in result.findings if f.reason_code is P.LABEL_OVERLAP} == {
        "p1",
        "s1",
    }


def test_the_combined_result_can_be_bound_to_the_rendered_image() -> None:
    combined = combine_map_results(
        _accuracy(GeoVerdict.PASS),
        _presentation(GeoVerdict.PASS),
        manifest_sha256="a" * 64,
        render_sha256="b" * 64,
    )
    assert (combined.manifest_sha256, combined.render_sha256) == ("a" * 64, "b" * 64)
    assert CombinedMapResult.model_validate_json(combined.model_dump_json()) == combined


def test_the_binding_is_optional_but_must_be_a_sha256_when_given() -> None:
    assert (
        combine_map_results(
            _accuracy(GeoVerdict.PASS), _presentation(GeoVerdict.PASS)
        ).render_sha256
        is None
    )
    with pytest.raises(ValidationError):
        combine_map_results(
            _accuracy(GeoVerdict.PASS), _presentation(GeoVerdict.PASS), render_sha256="nope"
        )
