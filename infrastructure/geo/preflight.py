"""Production の有料呼び出しの前に、指定された地図が通るかを確かめる（ADR-0084 §7）。

Render と**同じ関門**（``domain.geo.map_gate.gate_map``）を呼ぶ。決定論なので、ここで通った地図が
Render で別の結論になることはない（見た目の検査だけは Render の profile で行う）。通らなければ
``GeoMapRefusedError``（needs_input）で止め、fal（有料）を呼ぶ前に人間へ返す。
地図の指定が無い Episode は何もしない。同期（呼び出し側がスレッドで待つ）。
"""

from __future__ import annotations

from collections.abc import Sequence

from contracts.geo import GeoVerdict
from domain.errors import GeoMapRefusedError, GeoSourceInvalidError
from domain.geo.map_gate import gate_map
from infrastructure.geo.source import GeoSource


class GeoPreflight:
    def __init__(self, source: GeoSource) -> None:
        self._source = source

    def check(self, episode_id: str, scenes: Sequence[tuple[str, str]]) -> None:
        """``scenes`` は (storyboard_scene_id, script_scene_id) の列。"""
        data = self._source.load(episode_id)
        if data is None:
            return
        script_of = dict(scenes)
        refused: list[str] = []
        for spec in data.specs.specs:
            if script_of.get(spec.storyboard_scene_id) != spec.script_scene_id:
                raise GeoSourceInvalidError(
                    "a map spec points at a storyboard scene that is not in its script scene"
                )
            outcome = gate_map(spec, data.gazetteer, data.claims, data.sources)
            if outcome.passed:
                continue
            codes = sorted(
                {
                    f.reason_code.value
                    for f in outcome.accuracy.findings
                    if f.verdict is not GeoVerdict.PASS
                }
            )
            refused.append(
                f"{outcome.accuracy.scene_id}:{outcome.accuracy.verdict.value}:{','.join(codes)}"
            )
        if refused:
            raise GeoMapRefusedError("map refused before any paid call: " + "; ".join(refused))


__all__ = ["GeoPreflight"]
