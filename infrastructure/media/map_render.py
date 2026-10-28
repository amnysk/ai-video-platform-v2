"""GeoManifest から地図の PNG を決定論的に描く（ADR-0078、INV-119）。

同じ manifest・同じ ``MapRenderSpec``・同じフォントのバイト列から**同じ PNG のバイト列**を返す。
使うのは Pillow（既存依存、版は constraints.txt で固定）の ``ImageDraw`` だけ。固定するもの:
投影（``domain.geo.map_projection``）・キャンバス・色・フォントのサイズ・描画順（place_id 昇順）・
PNG の符号化（圧縮レベル固定・メタデータ無し）。乱数・時計・ネットワーク・画像生成 AI は使わない。

描かないもの: 国境線・地形（データを持たない）。点とラベル、模式図の免責だけ。正確さの判定は
``domain.geo.map_accuracy``、見た目の判定は ``domain.geo.map_presentation`` が**別に**行う。
``borders`` が ``none`` 以外でも線は描かない（描いていないものを「当時の国境」と見せない）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import PIL
from PIL import Image, ImageDraw, ImageFont

from contracts.artifact_refs import FrozenModel
from contracts.geo import GeoManifest, MapKind
from contracts.geo_map import LayoutText, MapLayout, PixelBox, Rgb
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.geo.map_projection import project_point

Color = tuple[int, int, int]
PNG_COMPRESS_LEVEL = 6
LABEL_PAD_PX = 6
MARKER_GAP_PX = 4


class MapRenderSpec(FrozenModel):
    """描画の固定値。ここを変えると画像が変わる（identity に入る）。"""

    width: int = 1920
    height: int = 1080
    margin_px: int = 64
    font_px: int = 36
    marker_radius_px: int = 8
    background: Rgb = Rgb(r=24, g=40, b=64)
    frame: Rgb = Rgb(r=200, g=200, b=200)
    marker: Rgb = Rgb(r=255, g=200, b=0)
    label_text: Rgb = Rgb(r=255, g=255, b=255)
    label_plate: Rgb = Rgb(r=0, g=0, b=0)


@dataclass(frozen=True, slots=True)
class RenderedMap:
    png: bytes
    sha256: str
    layout: MapLayout
    #: 地点 id → マーカー中心の px（テスト・診断用）
    marker_px: dict[str, tuple[int, int]]
    font_id: str
    #: 入力（manifest・spec・フォント）の指紋。画像の sha256 とは別
    identity_sha256: str
    #: manifest だけの指紋（判定結果を manifest に結びつける。``combine_map_results`` へ渡す）
    manifest_sha256: str


def _rgb(c: Rgb) -> Color:
    return (c.r, c.g, c.b)


def _load_font(
    font_bytes: bytes | None, size: int
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    if font_bytes is None:
        return ImageFont.load_default(size), f"pillow-default:{PIL.__version__}"
    try:
        font = ImageFont.truetype(io.BytesIO(font_bytes), size)
    except OSError as exc:
        raise ValueError("font bytes are not a usable font") from exc
    return font, f"font-sha256:{sha256_hex(font_bytes)}"


def _glyphs_missing(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> bool:
    """空白以外の字のうち、フォントの「無い字」の表示（notdef）と同じ形になるものがあるか。

    Pillow は無い字を例外にせず notdef の箱で描く。未割当の符号位置の形と一致すれば無いとみなす
    （保守的: 実在するが notdef と同形のグリフは無い前提）。
    """

    def mask(ch: str) -> bytes:
        return bytes(font.getmask(ch))  # type: ignore[arg-type]  # ImagingCore は buffer protocol

    notdef = mask("\U0010ffff")
    return any(not ch.isspace() and mask(ch) == notdef for ch in text)


def _text_size(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont
) -> tuple[int, int, int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, anchor="lt")
    return int(left), int(top), int(right - left), int(bottom - top)


def render_map(
    manifest: GeoManifest, spec: MapRenderSpec, font_bytes: bytes | None = None
) -> RenderedMap:
    font, font_id = _load_font(font_bytes, spec.font_px)
    image = Image.new("RGB", (spec.width, spec.height), _rgb(spec.background))
    draw = ImageDraw.Draw(image)
    half = spec.margin_px // 2
    draw.rectangle(
        (half, half, spec.width - half - 1, spec.height - half - 1), outline=_rgb(spec.frame)
    )

    places = sorted(manifest.places, key=lambda p: p.place_id)
    marker_px: dict[str, tuple[int, int]] = {}
    for place in places:
        marker_px[place.place_id] = project_point(
            manifest.bbox, spec.width, spec.height, spec.margin_px, place.lat, place.lon
        )
    r = spec.marker_radius_px
    for place in places:
        x, y = marker_px[place.place_id]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=_rgb(spec.marker))

    texts: list[LayoutText] = []
    for label in sorted(manifest.labels, key=lambda lb: (lb.place_id, lb.text)):
        if label.place_id not in marker_px:
            continue  # 未知の地点を指すラベルは正確さの判定が BLOCK する。描かない
        x, y = marker_px[label.place_id]
        off_x, off_y, w, h = _text_size(draw, label.text, font)
        plate_w, plate_h = w + 2 * LABEL_PAD_PX, h + 2 * LABEL_PAD_PX
        x0 = x + r + MARKER_GAP_PX
        if x0 + plate_w > spec.width - half:
            x0 = x - r - MARKER_GAP_PX - plate_w
        y0 = y - plate_h // 2
        draw.rectangle((x0, y0, x0 + plate_w - 1, y0 + plate_h - 1), fill=_rgb(spec.label_plate))
        draw.text(
            (x0 + LABEL_PAD_PX - off_x, y0 + LABEL_PAD_PX - off_y),
            label.text,
            font=font,
            fill=_rgb(spec.label_text),
            anchor="lt",
        )
        texts.append(
            LayoutText(
                target_id=label.place_id,
                box=PixelBox(x0=x0, y0=y0, x1=x0 + plate_w, y1=y0 + plate_h),
                font_px=spec.font_px,
                glyphs_missing=_glyphs_missing(font, label.text),
                text_rgb=spec.label_text,
                plate_rgb=spec.label_plate,
            )
        )

    schematic = manifest.map_kind is MapKind.SCHEMATIC
    disclaimer: LayoutText | None = None
    if schematic and manifest.schematic_disclaimer:
        text = manifest.schematic_disclaimer
        off_x, off_y, w, h = _text_size(draw, text, font)
        plate_w, plate_h = w + 2 * LABEL_PAD_PX, h + 2 * LABEL_PAD_PX
        x0, y0 = half + MARKER_GAP_PX, spec.height - half - MARKER_GAP_PX - plate_h
        draw.rectangle((x0, y0, x0 + plate_w - 1, y0 + plate_h - 1), fill=_rgb(spec.label_plate))
        draw.text(
            (x0 + LABEL_PAD_PX - off_x, y0 + LABEL_PAD_PX - off_y),
            text,
            font=font,
            fill=_rgb(spec.label_text),
            anchor="lt",
        )
        disclaimer = LayoutText(
            target_id=manifest.scene_id,
            box=PixelBox(x0=x0, y0=y0, x1=x0 + plate_w, y1=y0 + plate_h),
            font_px=spec.font_px,
            glyphs_missing=_glyphs_missing(font, text),
            text_rgb=spec.label_text,
            plate_rgb=spec.label_plate,
        )

    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=PNG_COMPRESS_LEVEL, optimize=False)
    png = buf.getvalue()
    layout = MapLayout(
        scene_id=manifest.scene_id,
        width=spec.width,
        height=spec.height,
        labels=tuple(texts),
        disclaimer=disclaimer,
        disclaimer_required=schematic,
    )
    identity = sha256_hex(
        canonical_json_bytes(
            {
                "manifest": manifest.model_dump(mode="json"),
                "spec": spec.model_dump(mode="json"),
                "font_id": font_id,
            }
        )
    )
    manifest_sha256 = sha256_hex(canonical_json_bytes(manifest.model_dump(mode="json")))
    return RenderedMap(
        png=png,
        sha256=sha256_hex(png),
        layout=layout,
        marker_px=marker_px,
        font_id=font_id,
        identity_sha256=identity,
        manifest_sha256=manifest_sha256,
    )


__all__ = ["PNG_COMPRESS_LEVEL", "MapRenderSpec", "RenderedMap", "render_map"]
