"""ADR-0087 の契約（音源ソースの方針・選曲の決定・BgmTrack の追加列）。

理由は docs/testing/bgm-auto-rationale.md（「契約」節）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.music import BgmTrack
from contracts.music_selection import (
    BGM_SELECTION_RULE_VERSION,
    FALLBACK_MAX_GAIN_STEPS,
    FALLBACK_MAX_TRACKS,
    BgmSelectionDecision,
    MusicRequirements,
)
from contracts.music_sources import (
    SOURCE_POLICIES,
    MusicSourceId,
    auto_acquire_allowed,
)

SHA = "a" * 64


def _track(**overrides: object) -> BgmTrack:
    base: dict[str, object] = {
        "track_id": "bgm-test",
        "file_sha256": SHA,
        "object_key": f"music/originals/{SHA}.flac",
        "title": "t",
        "artist": "a",
        "source_url": "https://commons.wikimedia.org/wiki/File:x.flac",
        "source_kind": "licensed_external",
        "license_type": "cc0",
        "license_status": "approved",
        "license_evidence_sha256": "b" * 64,
        "license_checked_at": datetime(2026, 11, 2, tzinfo=UTC),
        "allowed_platforms": ("youtube",),
        "allowed_formats": ("shorts", "longform"),
        "commercial_use_allowed": True,
        "reuse_allowed": True,
        "modification_allowed": True,
        "attribution_required": False,
        "energy": "low",
        "duration_ms": 120_000,
        "status": "active",
    }
    base.update(overrides)
    return BgmTrack.model_validate(base)


def test_only_wikimedia_commons_is_auto_acquirable() -> None:
    allowed = {s for s, p in SOURCE_POLICIES.items() if p.auto_acquire_allowed}
    assert allowed == {MusicSourceId.WIKIMEDIA_COMMONS}
    assert set(SOURCE_POLICIES) == set(MusicSourceId)
    for source in MusicSourceId:
        assert auto_acquire_allowed(source) is SOURCE_POLICIES[source].auto_acquire_allowed


def test_a_track_without_the_new_fields_still_parses_with_safe_defaults() -> None:
    track = _track()
    assert track.license_approver_kind is None
    assert track.content_id_risk == "unknown"
    assert track.mix_gain_offset_db == 0
    assert track.allowed_brands == ()


def test_a_policy_approval_must_name_its_policy_version() -> None:
    with pytest.raises(ValidationError):
        _track(license_approver_kind="policy")
    assert _track(license_approver_kind="policy", license_policy_version="v").license_policy_version


def test_gain_offset_has_a_bounded_range() -> None:
    with pytest.raises(ValidationError):
        _track(mix_gain_offset_db=13)
    assert _track(mix_gain_offset_db=6).mix_gain_offset_db == 6


def test_the_fallback_ladder_is_bounded() -> None:
    assert FALLBACK_MAX_TRACKS == 2
    assert FALLBACK_MAX_GAIN_STEPS == 1


def test_a_selection_decision_without_a_track_is_valid_no_bgm() -> None:
    req = MusicRequirements(
        brand_id="default",
        mood="calm",
        energy="low",
        tension="low",
        tempo_band="slow",
        culture="japanese",
        narration_density="normal",
        scene_changes_per_minute=6,
        duration_ms=42_000,
        fmt="shorts",
    )
    decision = BgmSelectionDecision(
        episode_id="00000000-0000-0000-0000-000000000001",
        rule_version=BGM_SELECTION_RULE_VERSION,
        requirements=req,
        requirements_sha256=SHA,
    )
    assert decision.track_id is None and decision.cue_sheet is None
