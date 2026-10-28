"""BGM カタログのインメモリ fake（ネットワーク・音源ファイルなし）。

ここの値は**テスト用の架空の事実**であり、実在の楽曲・ライセンスの判断ではない。
実際のライセンス判定は人の証拠確認が行う（ADR-0072）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.music import BgmTrack

NOW = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def sha(char: str) -> str:
    return char * 64


def make_track(track_id: str = "bgm-approved", sha_char: str = "a", **overrides: Any) -> BgmTrack:
    base: dict[str, Any] = {
        "track_id": track_id,
        "file_sha256": sha(sha_char),
        "object_key": f"music/originals/{sha(sha_char)}.mp3",
        "title": f"title {track_id}",
        "artist": "fixture artist",
        "source_url": "https://example.invalid/fixture",
        "source_kind": "licensed_external",
        "license_type": "custom",
        "license_status": "approved",
        "license_evidence_sha256": sha("e"),
        "license_checked_at": NOW - timedelta(days=1),
        "allowed_platforms": ("youtube",),
        "allowed_formats": ("shorts", "longform"),
        "commercial_use_allowed": True,
        "reuse_allowed": True,
        "modification_allowed": True,
        "attribution_required": False,
        "exact_attribution_text": None,
        "valid_until": None,
        "mood_tags": ("mysterious",),
        "genre_tags": ("ambient",),
        "energy": "low",
        "bpm": None,
        "duration_ms": 90_000,
        "loudness_lufs": None,
        "status": "active",
    }
    base.update(overrides)
    return BgmTrack(**base)


def catalog() -> tuple[BgmTrack, ...]:
    """承認済み・未確認・失効・Shorts 不可・帰属必須を 1 件ずつ含む。"""
    return (
        make_track("bgm-approved", "a"),
        make_track(
            "bgm-unverified",
            "b",
            license_status="unverified",
            license_evidence_sha256=None,
            license_checked_at=None,
        ),
        make_track("bgm-expired", "c", valid_until=NOW - timedelta(seconds=1)),
        make_track("bgm-longform-only", "d", allowed_formats=("longform",)),
        make_track(
            "bgm-credit",
            "f",
            attribution_required=True,
            exact_attribution_text='Music: "Fixture Song" by Fixture Artist (CC BY 4.0)',
        ),
    )
