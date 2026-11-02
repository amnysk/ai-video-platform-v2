"""自動選曲のテスト部品（架空の値。実在の曲・ライセンスの判断ではない）。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from contracts.artifacts import (
    ScriptArtifact,
    StoryboardArtifact,
    build_script_artifact,
    build_storyboard_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.music import BgmTrack
from domain.music.ports import CatalogEntry
from tests.support.bgm_catalog import NOW, make_track

EPISODE = "11111111-1111-4111-8111-111111111111"


def make_script(
    *,
    title: str = "題",
    hook: str = "つかみ",
    topic: str = "話題",
    narrations: tuple[str, ...] = ("静かな話。", "続きの話。", "おわり。"),
    language: str = "ja",
    scene_ms: int = 8000,
) -> ScriptArtifact:
    scenes = [
        {"id": f"s{i}", "narration": n, "visual": "v", "duration_ms": scene_ms}
        for i, n in enumerate(narrations, start=1)
    ]
    return parse_script_artifact(
        build_script_artifact(
            episode_id=EPISODE,
            language=language,
            title=title,
            hook=hook,
            scenes=scenes,
            metadata={"topic": topic, "generator": "fake", "generator_model": "fake"},
        )
    )


def make_storyboard(script: ScriptArtifact, *, per_scene: int = 1) -> StoryboardArtifact:
    rows: list[dict[str, Any]] = []
    start = 0
    for s in script.scenes:
        each = s.duration_ms // per_scene
        for _ in range(per_scene):
            i = len(rows) + 1
            rows.append(
                {
                    "scene_id": f"sb{i}",
                    "order": i,
                    "script_scene_id": s.id,
                    "start_ms": start,
                    "duration_ms": each,
                    "visual_kind": "broll",
                    "visual_description": "x",
                }
            )
            start += each
    return parse_storyboard_artifact(
        build_storyboard_artifact(
            episode_id=EPISODE,
            source_script={
                "artifact_id": str(uuid.uuid4()),
                "sha256": "a" * 64,
                "schema_version": "1.0",
            },
            scenes=rows,
            total_duration_ms=start,
            metadata={"generator": "fake", "generator_model": "fake", "generation_spec_id": "s"},
        )
    )


def entry_for(track: BgmTrack, **overrides: Any) -> CatalogEntry:
    base: dict[str, Any] = {
        "track": track,
        "acquired_from": "owner_supplied",
        "acquired_at": NOW - timedelta(days=2),
        "evidence_object_key": None,
        "analysis": {},
        "inferred_tags": {},
        "last_verified_at": None,
    }
    base.update(overrides)
    return CatalogEntry(**base)


def loud_track(track_id: str = "bgm-a", sha_char: str = "a", **kw: Any) -> BgmTrack:
    return make_track(track_id, sha_char, **{"loudness_lufs": -18.0, **kw})
