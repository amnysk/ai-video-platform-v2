"""Analytics → 特徴ごとの相対成績と信頼度（ADR-0025）。

「上位の Topic を真似る」のではなく theme / angle / era / subject の成績に分解する。
"""

from __future__ import annotations

from collections.abc import Mapping

from contracts.topic_planning import FeaturePerformance, MemoryItem, PlannerPolicy

from .ports import AnalyticsReport

#: 成績を学ぶ特徴の種類（``FeaturePerformance.feature`` の接頭辞）
FEATURE_KINDS: tuple[str, ...] = ("theme", "angle", "era", "subject")


def analytics_confidence(views: float, videos: int, policy: PlannerPolicy) -> float:
    """データ量に応じた 0..1。views と本数のどちらかが 0 なら 0。"""
    if views <= 0 or videos <= 0:
        return 0.0
    return min(1.0, views / policy.confidence_full_views) * min(
        1.0, videos / policy.confidence_full_videos
    )


def report_confidence(report: AnalyticsReport, policy: PlannerPolicy, window_days: int) -> float:
    videos = report.videos_by_window.get(window_days, [])
    return analytics_confidence(sum(v.views for v in videos), len(videos), policy)


def item_features(item: MemoryItem) -> list[str]:
    values = (item.theme, item.angle, item.era, item.subject)
    return [f"{kind}:{value}" for kind, value in zip(FEATURE_KINDS, values, strict=True) if value]


def derive_feature_performance(
    report: AnalyticsReport,
    features_by_video: Mapping[str, MemoryItem],
    window_days: int,
) -> list[FeaturePerformance]:
    """特徴ごとの 1 本あたり views ÷ チャンネル平均（全動画）。feature 名順。"""
    videos = report.videos_by_window.get(window_days, [])
    if not videos:
        return []
    channel_mean = sum(v.views for v in videos) / len(videos)
    if channel_mean <= 0:
        return []
    views_by_feature: dict[str, list[float]] = {}
    for v in videos:
        item = features_by_video.get(v.video_id)
        if item is None:
            continue
        for feature in item_features(item):
            views_by_feature.setdefault(feature, []).append(v.views)
    return [
        FeaturePerformance(
            feature=feature,
            relative_performance=(sum(views) / len(views)) / channel_mean,
            videos=len(views),
        )
        for feature, views in sorted(views_by_feature.items())
    ]
