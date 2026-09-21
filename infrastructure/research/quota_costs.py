"""YouTube Data API の quota 単位（公開 docs の値。**この repo では未検証**、ADR-0033 §5）。

Fake が本物と同じ規模の ``cost_units`` を返すために、Adapter と Fake が同じ値を参照する
（定義は 1 か所。AGENTS.md §8）。実行前に所有者が公式 docs で確認する。
"""

from __future__ import annotations

YOUTUBE_SEARCH_LIST_UNITS = 100
YOUTUBE_VIDEOS_LIST_UNITS = 1
YOUTUBE_CHANNELS_LIST_UNITS = 1
#: search.list + videos.list + channels.list を 1 回ずつ
YOUTUBE_FULL_SEARCH_UNITS = (
    YOUTUBE_SEARCH_LIST_UNITS + YOUTUBE_VIDEOS_LIST_UNITS + YOUTUBE_CHANNELS_LIST_UNITS
)
