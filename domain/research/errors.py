"""Research の失敗（ADR-0037）。分類は ``domain.errors`` の基底クラスで決まる。

``domain/errors.py``（本番の失敗の表）には足さない。isinstance による分類
（``classify_failure``）はそのまま働く。型名だけで分類する経路（Temporal の workflow 側）に
research の例外を載せる段（Worker、ADR-0037 §8）で、型名の扱いを決める。
"""

from __future__ import annotations

from domain.errors import NeedsInputError, PermanentError


class ResearchIdempotencyConflictError(PermanentError):
    """同じ冪等キーで内容が違う依頼・呼び出しが来た。保存済みの行は変えない。"""


class ResearchBudgetExceededError(NeedsInputError):
    """呼び出し件数・金額・quota の上限に達した（INV-36）。

    再送しても上限は増えない。所有者が上限を決め直して新しい依頼を出す。
    """


class ResearchAmbiguousCallError(NeedsInputError):
    """外部呼び出しの成否が不明（dispatch 済みで結果が無い）。

    同じ入力を再送しない・枠を手放さない。人手照合を待つ（ADR-0013 と同じ意味論）。
    """


__all__ = [
    "ResearchAmbiguousCallError",
    "ResearchBudgetExceededError",
    "ResearchIdempotencyConflictError",
]
