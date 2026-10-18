"""Data API 読み取りの解釈（ADR-0047 §5）。理由は docs/testing/agent-tools-rationale.md。"""

from __future__ import annotations

import pytest

from infrastructure.youtube.data_reader import (
    DATA_API_READ_URLS,
    parse_duration,
    parse_playlist_page,
    parse_videos,
)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT30S", 30.0), ("PT1M3S", 63.0), ("PT1H2M3S", 3723.0), ("P0D", 0.0), ("P1DT1S", 86401.0)],
)
def test_parse_duration(value: str, seconds: float) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "P", "PT", "1:03", None, 63])
def test_unparseable_duration_is_none_not_zero(value: object) -> None:
    assert parse_duration(value) is None


def test_parse_videos_skips_items_without_a_readable_publish_date() -> None:
    body = {
        "items": [
            {"id": "a", "snippet": {"publishedAt": "2026-09-01T23:30:00+09:00"}},
            {"id": "b", "snippet": {"publishedAt": "garbage"}},
        ]
    }
    out = parse_videos(body)
    assert set(out) == {"a"}
    assert out["a"].published_date.isoformat() == "2026-09-01"  # UTC に正規化（14:30Z）
    assert out["a"].live_broadcast_content == "none"


def test_playlist_page_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        parse_playlist_page([])


def test_only_three_read_endpoints_exist() -> None:
    assert {u.rsplit("/", 1)[1] for u in DATA_API_READ_URLS} == {
        "videos",
        "channels",
        "playlistItems",
    }
