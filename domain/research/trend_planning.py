"""Trend の検索計画（ADR-0039 §1）。純粋・決定的（同じ入力なら同じ出力）。

``plan_trend_searches``: 依頼（地域・言語・形式・期間・seed 語）から ``SearchStep`` を
優先順に組む。

- YouTube（``kind="youtube"``）と Web（``kind="web"``）の両方。seed 語（無ければ
  「日本史」）に加え、関連する映画・ゲーム・展示・ドラマの語も探す
- 期間は**直近 7 日**と**直近 30 日**を使い分け、``published_after`` に**絶対日時**を
  入れる（依頼の ``time_window`` の外へは出ない）。同じ語を 2 つの窓で探すので、同じ動画を
  2 時点で観測しうる（``views_per_hour_delta`` の材料。Provider が観測時刻を返す）
- ``region_code`` / ``language`` は**絞り込み・重みづけ**で、人気・言語の断定ではない
- ``videoDuration`` は使わない（Shorts の判定にならない）。形式が ``shorts`` の依頼は
  YouTube の検索語に ``shorts`` を足すだけ（**検索のヒント**であって判定ではない）
- ``max_searches`` を超えない（超える分は落とす。落とした分は ``partial`` にしない:
  計画の優先順で上限まで探すのが Trend の仕様）

Trend は**本文を取得しない**（``select_fetches`` は空）。統計は検索結果
（``SearchHit.stats``）にあり、テーマの適合は見出しと snippet で見る
（取得の枠・費用を使わない。ADR-0039 §1）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from contracts.research import TrendResearchRequest, normalize_claim_text
from domain.research.handlers import SearchStep
from domain.research.ports import SearchKind, SearchQuery

__all__ = [
    "CROSS_MEDIA_TERMS",
    "DOMAIN_BASE_TERMS",
    "RECENT_WINDOW_DAYS",
    "SHORT_WINDOW_DAYS",
    "WEB_MAX_RESULTS",
    "YOUTUBE_MAX_RESULTS",
    "plan_trend_searches",
]

#: 「直近」の窓（日）。``published_after = window.end - N 日``（依頼の ``start``
#: より前へは行かない）
SHORT_WINDOW_DAYS = 7
RECENT_WINDOW_DAYS = 30

YOUTUBE_MAX_RESULTS = 10
WEB_MAX_RESULTS = 5

#: 日本史そのものを表す語（seed 語が無いときの主題。周辺の話題探索にも使う）
DOMAIN_BASE_TERMS: dict[str, str] = {"ja": "日本史", "en": "Japanese history"}

#: 日本史と関連する話題（ニュース・映画・ゲーム・展示・ドラマ）を探すための語。順序が優先順
CROSS_MEDIA_TERMS: dict[str, tuple[str, ...]] = {
    "ja": ("ニュース", "映画", "ゲーム", "展示", "ドラマ"),
    "en": ("news", "movie", "game", "exhibition", "drama"),
}

#: 形式が shorts の依頼で YouTube 検索へ足す語。**検索のヒント**であって Shorts の判定ではない
_SHORTS_HINT = "shorts"

_Plan = tuple[SearchKind, str, int]  # (kind, text, window_days)


def _candidate_plans(spec: TrendResearchRequest) -> list[_Plan]:
    """優先順の検索案（重複は後で落とす）。"""
    language = spec.language
    base = DOMAIN_BASE_TERMS[language]
    subjects = tuple(spec.inputs.seed_terms) or (base,)
    cross = CROSS_MEDIA_TERMS[language]
    news = cross[0]
    first = subjects[0]

    plans: list[_Plan] = [
        ("youtube", first, SHORT_WINDOW_DAYS),
        ("web", f"{first} {news}", RECENT_WINDOW_DAYS),
        ("youtube", first, RECENT_WINDOW_DAYS),
    ]
    for subject in subjects[1:]:
        plans.append(("youtube", subject, SHORT_WINDOW_DAYS))
        plans.append(("web", f"{subject} {news}", RECENT_WINDOW_DAYS))
    for word in cross[1:]:
        plans.append(("web", f"{base} {word}", RECENT_WINDOW_DAYS))
    plans.append(("youtube", base, SHORT_WINDOW_DAYS))
    for subject in subjects[1:]:
        plans.append(("youtube", subject, RECENT_WINDOW_DAYS))
    return plans


def plan_trend_searches(spec: TrendResearchRequest, *, max_searches: int) -> tuple[SearchStep, ...]:
    """検索を優先順に返す（``max_searches`` 件まで。決定的）。"""
    if max_searches < 1:
        return ()
    window = spec.time_window
    steps: list[SearchStep] = []
    seen: set[tuple[SearchKind, str, datetime]] = set()
    for kind, text, days in _candidate_plans(spec):
        after = max(window.start, window.end - timedelta(days=days))
        query_text = text
        if kind == "youtube" and spec.format_profile == "shorts":
            query_text = f"{text} {_SHORTS_HINT}"
        key = (kind, normalize_claim_text(query_text), after)
        if key in seen:
            continue
        seen.add(key)
        steps.append(
            SearchStep(
                step_id=f"s{len(steps) + 1:02d}",
                query=SearchQuery(
                    text=query_text,
                    kind=kind,
                    max_results=YOUTUBE_MAX_RESULTS if kind == "youtube" else WEB_MAX_RESULTS,
                    region_code=spec.inputs.region,
                    language=spec.language,
                    published_after=after,
                    published_before=window.end,
                ),
                purpose=f"trend:{text}"[:200],
            )
        )
        if len(steps) >= max_searches:
            break
    return tuple(steps)
