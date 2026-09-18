"""Topic Planner の純粋ドメイン（ADR-0025）。I/O・SDK・DB を持たない。"""

from .analytics import (
    analytics_confidence,
    derive_feature_performance,
    item_features,
    report_confidence,
)
from .duplicates import DuplicateResult, classify_duplicate, similarity
from .normalize import jaccard, normalize_title
from .ports import AnalyticsProvider, AnalyticsReport, AudienceShares, VideoMetrics
from .scoring import (
    COMPONENTS,
    analytics_fit,
    effective_weights,
    portfolio_balance,
    score_candidate,
    us_young_fit,
)
from .selection import Evaluation, SelectionResult, select

__all__ = [
    "COMPONENTS",
    "AnalyticsProvider",
    "AnalyticsReport",
    "AudienceShares",
    "DuplicateResult",
    "Evaluation",
    "SelectionResult",
    "VideoMetrics",
    "analytics_confidence",
    "analytics_fit",
    "classify_duplicate",
    "derive_feature_performance",
    "effective_weights",
    "item_features",
    "jaccard",
    "normalize_title",
    "portfolio_balance",
    "report_confidence",
    "score_candidate",
    "select",
    "similarity",
    "us_young_fit",
]
