"""判定語彙 PASS / WARN / REVIEW / BLOCK と厳しさの順序の**唯一の定義**（INV-105 / ADR-0074）。

``contracts.geo.GeoVerdict`` と ``contracts.quality.QualityVerdict`` は、この ``Verdict`` の別名
（同一オブジェクト）。値（``"pass"`` 等）は従来と同じで、JSON・pydantic の直列化は変わらない。
重さの順は PASS < WARN < REVIEW < BLOCK。順序は ``VERDICT_ORDER`` だけが持つ。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Verdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    REVIEW = "review"
    BLOCK = "block"


#: 軽い方から重い方への並び。**厳しさの順序の唯一の定義**。
VERDICT_ORDER: tuple[Verdict, ...] = (
    Verdict.PASS,
    Verdict.WARN,
    Verdict.REVIEW,
    Verdict.BLOCK,
)

#: 順位（0 = PASS … 3 = BLOCK）。``VERDICT_ORDER`` から導出する。
VERDICT_SEVERITY: dict[Verdict, int] = {v: i for i, v in enumerate(VERDICT_ORDER)}


def stricter(a: Verdict, b: Verdict) -> Verdict:
    """厳しい方。同じ重さなら ``a``。"""
    return a if VERDICT_SEVERITY[a] >= VERDICT_SEVERITY[b] else b


def worst(verdicts: Iterable[Verdict]) -> Verdict:
    """最も厳しい verdict。空なら PASS（呼び出し側が「無検査は PASS ではない」を別に守る）。"""
    return max(verdicts, key=VERDICT_SEVERITY.__getitem__, default=Verdict.PASS)
