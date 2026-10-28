"""検査結果の集計（INV-102）。

- 決定論の検査（``deterministic=True``）だけが BLOCK を作れる
- LLM の検査は判定を**厳しくする**方向にだけ働く。上限は REVIEW（人の確認）。
  決定論の BLOCK / REVIEW を緩めることはできない（LLM の PASS は何も足さない）
- 決定論の検査が 1 つも無ければ PASS にしない（LLM の自己採点だけでは通らない）
- 検査が空なら REVIEW（無検査は PASS ではない / INV-101）
"""

from __future__ import annotations

from collections.abc import Iterable

from contracts.quality import QualityCheck, QualityVerdict
from contracts.verdict import worst as _worst


def aggregate(checks: Iterable[QualityCheck]) -> QualityVerdict:
    items = tuple(checks)
    deterministic = [c.verdict for c in items if c.deterministic]
    llm = [
        QualityVerdict.REVIEW if c.verdict is QualityVerdict.BLOCK else c.verdict
        for c in items
        if not c.deterministic
    ]
    if not deterministic:
        # 決定論の根拠が無い: LLM が何と言おうと PASS/WARN にはしない
        return _worst([QualityVerdict.REVIEW, *llm])
    return _worst([*deterministic, *llm])
