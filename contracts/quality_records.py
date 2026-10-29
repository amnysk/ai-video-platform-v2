"""品質検査の記録の語彙（ADR-0081）。**唯一の定義**（AGENTS.md §8）。

検査は ``CheckKey`` 単位の「セット」として保存される。各セットは対応する ``QualityGate`` を
**すべて**含まなければならず、含まないセットは書けない・読んでも無効（``CHECK_KEY_GATES``）。
BGM・地図は「使われたとき必須、使われないときは明示の不使用宣言が必須」（``UsageFeature``）。
純粋: I/O を持たない。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from contracts.quality import QualityGate


class CheckKey(StrEnum):
    """検査セットの種類。DB の ``check_key`` 列の語彙でもある。"""

    TECHNICAL_QA = "technical_qa"
    DESCRIPTION_LINT = "description_lint"
    BGM = "bgm"
    MAP = "map"


class UsageFeature(StrEnum):
    """完成動画で使われたかを render が宣言する機能。DB の ``feature`` 列の語彙。"""

    BGM = "bgm"
    MAP = "map"


#: 検査セットが含まなければならない gate（過不足なし）。
CHECK_KEY_GATES: Mapping[CheckKey, frozenset[QualityGate]] = {
    CheckKey.TECHNICAL_QA: frozenset({QualityGate.VISUAL, QualityGate.AUDIO}),
    CheckKey.DESCRIPTION_LINT: frozenset({QualityGate.METADATA}),
    CheckKey.BGM: frozenset({QualityGate.MUSIC, QualityGate.LICENSE}),
    CheckKey.MAP: frozenset({QualityGate.GEO}),
}

#: 使用の有無に関わらず常に必須の検査。
ALWAYS_REQUIRED_KEYS: tuple[CheckKey, ...] = (CheckKey.TECHNICAL_QA, CheckKey.DESCRIPTION_LINT)

#: 機能が使われたときに必須になる検査。
FEATURE_CHECK_KEY: Mapping[UsageFeature, CheckKey] = {
    UsageFeature.BGM: CheckKey.BGM,
    UsageFeature.MAP: CheckKey.MAP,
}

#: 「宣言された不使用」の PASS 検査と、欠落の REVIEW 検査の ``detail``（固定文。INV-106）。
DETAIL_DECLARED_NOT_USED = "feature declared not used for this final video"
DETAIL_REQUIRED_MISSING = "required check set is missing or not fresh"
DETAIL_USAGE_MISSING = "usage of this feature was not declared"
