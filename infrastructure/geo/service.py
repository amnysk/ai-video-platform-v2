"""Render 用の窓口（ADR-0084 §4 / §7）。worker は geo の契約を import せず、これだけを呼ぶ。

流れ（すべて ``RenderActivities`` の既存 Activity の内側。workflow・Activity の追加/変更なし）:

1. ``prepare``: 供給元を読み、地図ごとに関門→描画→見た目の検査。同期・CPU のみ。
   結果の ``GeoPlan.identity`` は ``render_input_hash(geo=...)`` に入る。
2. ``GeoPlan.materialize``: 作業領域で PASS の地図だけクリップ（mp4）にし、そのシーンの動画の
   入力パスを差し替える。BLOCK / REVIEW の地図は差し替えない（従来の動画のまま）。
3. ``GeoPlan.evidence``: 完成動画の sha256 が決まったあと、証跡（``GeoRenderEvidence``）を作る。
"""

from __future__ import annotations

import hashlib
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.geo import GeoVerdict
from contracts.geo_scene import (
    CLIP_DURATION_MAX_MS,
    GEO_EVIDENCE_OBJECT_TYPE,
    GEO_SCENE_SCHEMA_VERSION,
    GeoMapRecord,
    GeoRenderEvidence,
)
from contracts.render import RenderProfile
from domain.artifact.keys import artifact_object_key
from domain.errors import GeoSourceInvalidError
from domain.geo.digest import sha256_of
from domain.geo.map_binding import evidence_binding_id
from infrastructure.geo.clip import MAP_CLIP_VERSION, MapClipEncoder
from infrastructure.geo.prepare import PreparedMap, prepare_map
from infrastructure.geo.source import GeoSource
from infrastructure.media.map_render import MapRenderSpec

#: identity の版。identity に入れる項目を変えたら上げる
GEO_IDENTITY_VERSION = "1"
MARGIN_RATIO = 0.06
FONT_WIDTH_RATIO = 0.042
MARKER_WIDTH_RATIO = 0.009


@dataclass(frozen=True, slots=True)
class SceneSlot:
    """時間軸上の storyboard シーン 1 件（Render が組んだ計画から導く）。"""

    storyboard_scene_id: str
    script_scene_id: str
    #: シーン動画 Artifact の実尺。クリップはこの尺で作る（計画の trim / freeze が同じに効く）
    source_duration_ms: int


def render_spec_for(profile: RenderProfile) -> MapRenderSpec:
    """profile の解像度で描く（拡縮しない）。比率は幅に対する割合で決定的。"""
    return MapRenderSpec(
        width=profile.width,
        height=profile.height,
        margin_px=round(profile.width * MARGIN_RATIO),
        font_px=round(profile.width * FONT_WIDTH_RATIO),
        marker_radius_px=max(6, round(profile.width * MARKER_WIDTH_RATIO)),
    )


def geo_evidence_key(episode_id: str, final_video_sha256: str, input_hash: str) -> str:
    return artifact_object_key(
        episode_id, GEO_EVIDENCE_OBJECT_TYPE, evidence_binding_id(final_video_sha256, input_hash)
    )


@dataclass(slots=True)
class GeoPlan:
    episode_id: str
    profile: RenderProfile
    prepared: tuple[PreparedMap, ...]
    #: ``render_input_hash(geo=...)`` に入れる辞書。地図が無くても必ずある（ON の印）
    identity: dict[str, object]
    encoder: MapClipEncoder
    durations: dict[str, int]
    _clips: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def applied(self) -> tuple[PreparedMap, ...]:
        return tuple(p for p in self.prepared if p.applied)

    @property
    def refused(self) -> tuple[PreparedMap, ...]:
        return tuple(p for p in self.prepared if not p.applied)

    def materialize(self, video_paths: MutableMapping[str, Path], work_dir: Path) -> None:
        """PASS の地図を mp4 にして、そのシーンの入力動画を差し替える。"""
        for prepared in self.applied:
            sb = prepared.record.storyboard_scene_id
            assert prepared.png is not None
            folder = work_dir / f"geo-{sb}"
            folder.mkdir(parents=True, exist_ok=True)
            png, clip = folder / "map.png", folder / "map.mp4"
            png.write_bytes(prepared.png)
            if hashlib.sha256(prepared.png).hexdigest() != prepared.record.png_sha256:
                raise GeoSourceInvalidError("map image does not match its record")
            duration = self.durations[sb]
            self.encoder.encode(png, clip, duration, self.profile)
            video_paths[sb] = clip
            self._clips[sb] = (hashlib.sha256(clip.read_bytes()).hexdigest(), duration)

    def records(self, final_video_sha256: str, input_hash: str) -> tuple[GeoMapRecord, ...]:
        out: list[GeoMapRecord] = []
        for prepared in self.prepared:
            rec = prepared.record
            update: dict[str, Any] = {
                "final_video_sha256": final_video_sha256,
                "input_hash": input_hash,
            }
            clip = self._clips.get(rec.storyboard_scene_id)
            if prepared.applied:
                if clip is None:
                    raise GeoSourceInvalidError("an applied map has no clip")
                update["clip_sha256"], update["clip_duration_ms"] = clip
            out.append(GeoMapRecord.model_validate({**rec.model_dump(mode="json"), **update}))
        return tuple(out)

    def evidence(self, final_video_sha256: str, input_hash: str) -> GeoRenderEvidence:
        records = self.records(final_video_sha256, input_hash)
        return GeoRenderEvidence(
            schema_version=GEO_SCENE_SCHEMA_VERSION,
            episode_id=self.episode_id,
            final_video_sha256=final_video_sha256,
            input_hash=input_hash,
            intended_scene_ids=tuple(sorted(r.scene_id for r in records)),
            records=records,
        )


class GeoRenderService:
    def __init__(
        self, *, source: GeoSource, encoder: MapClipEncoder, font_path: Path | None
    ) -> None:
        self._source = source
        self._encoder = encoder
        self._font_path = font_path

    def prepare(
        self, episode_id: str, slots: Sequence[SceneSlot], profile: RenderProfile
    ) -> GeoPlan:
        data = self._source.load(episode_id)
        base_identity: dict[str, object] = {"version": GEO_IDENTITY_VERSION}
        if data is None:
            return GeoPlan(
                episode_id=episode_id,
                profile=profile,
                prepared=(),
                identity={**base_identity, "maps": []},
                encoder=self._encoder,
                durations={},
            )
        by_sb = {s.storyboard_scene_id: s for s in slots}
        durations: dict[str, int] = {}
        for spec in data.specs.specs:
            slot = by_sb.get(spec.storyboard_scene_id)
            if slot is None or slot.script_scene_id != spec.script_scene_id:
                raise GeoSourceInvalidError(
                    "a map spec points at a storyboard scene that is not in its script scene"
                )
            if not 0 < slot.source_duration_ms <= CLIP_DURATION_MAX_MS:
                raise GeoSourceInvalidError("the scene video duration is out of the clip range")
            durations[spec.storyboard_scene_id] = slot.source_duration_ms
        # 本番は固定したフォント（render worker が sha256 検証済み）。None は ASCII だけのテスト用
        font_bytes = self._font_path.read_bytes() if self._font_path is not None else None
        render_spec = render_spec_for(profile)
        prepared = tuple(
            prepare_map(spec, data, render_spec, font_bytes, render_profile_id=profile.profile_id)
            for spec in sorted(data.specs.specs, key=lambda s: s.storyboard_scene_id)
        )
        maps = [
            {
                "storyboard_scene_id": p.record.storyboard_scene_id,
                "verdict": p.record.verdict.value,
                "manifest_sha256": p.record.manifest_sha256,
                "verification_sha256": p.record.verification_sha256,
                "png_sha256": p.record.png_sha256,
                "gazetteer_sha256": p.record.gazetteer_sha256,
                "evidence_sha256": p.record.evidence_sha256,
                "applied": p.applied,
                "clip_duration_ms": durations[p.record.storyboard_scene_id] if p.applied else None,
            }
            for p in prepared
        ]
        identity = {
            **base_identity,
            "clip_version": MAP_CLIP_VERSION,
            "font_sha256": hashlib.sha256(font_bytes).hexdigest() if font_bytes else None,
            "maps": maps,
            "specs_sha256": sha256_of(data.specs),
        }
        return GeoPlan(
            episode_id=episode_id,
            profile=profile,
            prepared=prepared,
            identity=identity,
            encoder=self._encoder,
            durations=durations,
        )


def refusal_summary(plan: GeoPlan) -> list[tuple[str, str, list[str]]]:
    """拒否された地図の (scene_id, verdict, 理由コード)。ログ用（入力の写しなし）。"""
    out = []
    for p in plan.refused:
        codes = sorted(
            {
                f.reason_code.value
                for f in p.record.accuracy.findings
                if f.verdict is not GeoVerdict.PASS
            }
            | (
                {
                    f.reason_code.value
                    for f in p.record.presentation.findings
                    if f.verdict is not GeoVerdict.PASS
                }
                if p.record.presentation is not None
                else set()
            )
        )
        out.append((p.record.scene_id, p.record.verdict.value, codes))
    return out


__all__ = [
    "GEO_IDENTITY_VERSION",
    "GeoPlan",
    "GeoRenderService",
    "SceneSlot",
    "geo_evidence_key",
    "refusal_summary",
    "render_spec_for",
]
