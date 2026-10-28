"""BGM 結線テストの部品。値は架空（実在の曲・ライセンスの判断ではない）。"""

from __future__ import annotations

from typing import Any

from contracts.music import BgmTrack, MusicCue, MusicCueSheet
from contracts.render import RenderPlan
from tests.support.bgm_catalog import make_track, sha
from tests.support.render_plans import make_plan

CREDIT_TEXT = 'Music: "Fixture Song" by Fixture Artist (CC BY 4.0)'


def bgm_plan(profile: str = "shorts_vertical") -> RenderPlan:
    """2 シーン（各 2 秒、計 4 秒）・ナレーション s1 / s2。"""
    return make_plan(
        profile=profile,
        scenes=((2000, 2000), (2000, 2000)),
        voices=((0, 1500), (2200, 1500)),
        subtitles=False,
    )


def cue(**overrides: Any) -> MusicCue:
    base: dict[str, Any] = {
        "scene_id": "s1",
        "track_id": "bgm-approved",
        "source_sha256": sha("a"),
        "start_ms": 0,
        "end_ms": 2_000,
        "fade_in_ms": 200,
        "fade_out_ms": 300,
        "target_relative_level_db": -18,
        "ducking_policy": "duck_under_voice",
        "license_evidence_hash": sha("e"),
        "credits_text": None,
    }
    base.update(overrides)
    return MusicCue(**base)


def sheet(*cues: MusicCue) -> MusicCueSheet:
    return MusicCueSheet(cues=cues or (cue(),))


def credit_track() -> BgmTrack:
    return make_track(
        "bgm-credit", "f", attribution_required=True, exact_attribution_text=CREDIT_TEXT
    )


def file_shas(*tracks: BgmTrack) -> dict[str, str | None]:
    return {t.track_id: t.file_sha256 for t in tracks}
