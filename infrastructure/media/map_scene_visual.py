"""地図をシーン映像の入力にする入口（ADR-0078、INV-120）。flag は引数で受ける。

``enabled`` が False（``Settings.geo_maps_enabled`` の既定）なら**何もせず** ``None``。
呼び出し側（後続の接続）は ``None`` のとき従来どおりの経路を使う。
この関数は workflow・Activity から呼ばれない。
フォントを渡さないと既定フォント（ASCII のみ）で描く。足りない字は黙って豆腐にせず、
``RenderedMap.layout`` の ``glyphs_missing`` → 見た目の検査の BLOCK（FONT_MISSING_GLYPHS）で止まる。
"""

from __future__ import annotations

from contracts.geo import GeoManifest
from infrastructure.media.map_render import MapRenderSpec, RenderedMap, render_map


def render_map_scene_visual(
    *,
    enabled: bool,
    manifest: GeoManifest,
    spec: MapRenderSpec,
    font_bytes: bytes | None = None,
) -> RenderedMap | None:
    if not enabled:
        return None
    return render_map(manifest, spec, font_bytes)


__all__ = ["render_map_scene_visual"]
