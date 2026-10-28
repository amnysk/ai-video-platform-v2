"""地図の見た目（視認性）の検査（ADR-0078、INV-118）。純関数（INV-6）。

``MapLayout``（描画器が返す幾何）だけを読む。正確さ（``map_accuracy``）とは別の関数・別の理由コードで、
結果は ``combine_map_results`` が ``stricter`` でしか合流しない: 見た目が良くても正確さの
BLOCK / REVIEW は消えない。見た目の最悪は REVIEW（読めない地図は人が見る）。例外は模式図の免責が
読めない・画面外・無いとき（INV-96 の表示要件）で BLOCK。
"""

from __future__ import annotations

from contracts.geo import GeoVerdict, stricter
from contracts.geo_map import (
    CombinedMapResult,
    LayoutText,
    MapAccuracyResult,
    MapLayout,
    MapPresentationResult,
    PixelBox,
    PresentationFinding,
    PresentationReasonCode,
    Rgb,
)

#: 初期値（実データで調整する。ADR-0078）。**定義はここだけ**。
MIN_LABEL_FONT_PX = 28
MIN_CONTRAST_RATIO = 4.5

P = PresentationReasonCode


def _linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color: Rgb) -> float:
    return 0.2126 * _linear(color.r) + 0.7152 * _linear(color.g) + 0.0722 * _linear(color.b)


def contrast_ratio(a: Rgb, b: Rgb) -> float:
    """WCAG 2.x のコントラスト比（1.0〜21.0。引数の順に依らない）。"""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _overlaps(a: PixelBox, b: PixelBox) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def _inside(box: PixelBox, width: int, height: int) -> bool:
    return box.x0 >= 0 and box.y0 >= 0 and box.x1 <= width and box.y1 <= height


def _text_problems(item: LayoutText, width: int, height: int) -> list[PresentationReasonCode]:
    out: list[PresentationReasonCode] = []
    if item.font_px < MIN_LABEL_FONT_PX:
        out.append(P.FONT_TOO_SMALL)
    if contrast_ratio(item.text_rgb, item.plate_rgb) < MIN_CONTRAST_RATIO:
        out.append(P.LOW_CONTRAST)
    if not _inside(item.box, width, height):
        out.append(P.LABEL_OUTSIDE_CANVAS)
    if item.glyphs_missing:
        out.append(P.FONT_MISSING_GLYPHS)
    return out


def _verdict_for(code: PresentationReasonCode) -> GeoVerdict:
    """読めない（豆腐）地図は止める。他の見た目の問題は人が見る。"""
    return GeoVerdict.BLOCK if code is P.FONT_MISSING_GLYPHS else GeoVerdict.REVIEW


def check_map_presentation(layout: MapLayout) -> MapPresentationResult:
    findings: list[PresentationFinding] = []
    for item in layout.labels:
        findings.extend(
            PresentationFinding(
                verdict=_verdict_for(code), reason_code=code, target_id=item.target_id
            )
            for code in _text_problems(item, layout.width, layout.height)
        )
    overlapping: set[str] = set()
    for i, a in enumerate(layout.labels):
        for b in layout.labels[i + 1 :]:
            if _overlaps(a.box, b.box):
                overlapping.update((a.target_id, b.target_id))
    findings.extend(
        PresentationFinding(
            verdict=GeoVerdict.REVIEW, reason_code=P.LABEL_OVERLAP, target_id=target
        )
        for target in sorted(overlapping)
    )
    disclaimer = layout.disclaimer
    if layout.disclaimer_required:
        if disclaimer is None or _text_problems(disclaimer, layout.width, layout.height):
            findings.append(
                PresentationFinding(
                    verdict=GeoVerdict.BLOCK,
                    reason_code=P.DISCLAIMER_NOT_VISIBLE,
                    target_id=layout.scene_id,
                )
            )
    elif disclaimer is not None:
        findings.extend(
            PresentationFinding(
                verdict=_verdict_for(code), reason_code=code, target_id=layout.scene_id
            )
            for code in _text_problems(disclaimer, layout.width, layout.height)
        )
    if not findings:
        findings.append(
            PresentationFinding(
                verdict=GeoVerdict.PASS, reason_code=P.OK, target_id=layout.scene_id
            )
        )
    worst = GeoVerdict.PASS
    for f in findings:
        worst = stricter(worst, f.verdict)
    return MapPresentationResult(scene_id=layout.scene_id, verdict=worst, findings=tuple(findings))


def combine_map_results(
    accuracy: MapAccuracyResult, presentation: MapPresentationResult
) -> CombinedMapResult:
    """正確さと見た目の合流。結果は両者の厳しい側。点数・verdict 単体は受け取らない。"""
    if not isinstance(accuracy, MapAccuracyResult) or not isinstance(
        presentation, MapPresentationResult
    ):
        raise TypeError("combine_map_results takes the two structured results only")
    return CombinedMapResult(
        scene_id=accuracy.scene_id,
        verdict=stricter(accuracy.verdict, presentation.verdict),
        accuracy=accuracy,
        presentation=presentation,
    )


__all__ = [
    "MIN_CONTRAST_RATIO",
    "MIN_LABEL_FONT_PX",
    "check_map_presentation",
    "combine_map_results",
    "contrast_ratio",
]
