"""Research の失敗（ADR-0037）。分類は ``domain.errors`` の基底クラスで決まる。

``domain/errors.py``（本番の失敗の表）には足さない。isinstance による分類
（``classify_failure``）はそのまま働く。

**型名による分類（Temporal）**: ``domain.errors.FAILURE_CLASS_BY_TYPE_NAME`` は
``domain/errors.py`` の import 時に継承から作られるので、ここで定義した型は載らない（その表だけで
引くと安全側の ``needs_input`` に落ち、``NON_RETRYABLE_ERROR_TYPE_NAMES`` にも入らない）。
そこで research 側の表 ``RESEARCH_FAILURE_CLASS_BY_TYPE_NAME`` を**このモジュールの import 時に、
同じ継承の規則から**作る（ADR-0037 §8）。

- research の例外は**このモジュールにだけ**定義する（別のモジュールに置くと表に載らない。
  ``tests/unit/test_research_execution_domain.py`` が検査する）
- 各型の失敗クラスは、MRO の中で最初に現れる ``domain.errors`` の型の失敗クラス
  （基底の表を引くだけで、失敗クラスをここに書き直さない。AGENTS.md §8）
- research の Worker（Temporal の RetryPolicy）は ``NON_RETRYABLE_ERROR_TYPE_NAMES`` と
  ``RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES`` の和を渡し、workflow 側は
  ``research_failure_class_from_type_name`` で引く
"""

from __future__ import annotations

from contracts.states import FailureClass
from domain.errors import (
    FAILURE_CLASS_BY_TYPE_NAME,
    DomainError,
    NeedsInputError,
    PermanentError,
    RetryableError,
    failure_class_from_type_name,
)


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


class ResearchSourceUnavailableError(RetryableError):
    """外部呼び出しが一時障害で失敗した（5xx・通信断・timeout）。

    呼び出しは ``spent`` として記録済み。Temporal の retry は新しい番号の予約を取るので、
    retry も合計で上限を数える（INV-36）。
    """


class ResearchArtifactReadbackError(RetryableError):
    """成果物を書いた後の読み戻しの sha256 が一致しない。記録しない（現行にしない）。"""


class ResearchProviderNotConfiguredError(NeedsInputError):
    """Provider が ``none`` / 未設定。所有者が Provider を選ぶまで実行しない（ADR-0036 §3）。"""


class ResearchProviderAuthError(NeedsInputError):
    """認証・認可の拒否、または quota の枯渇。人手か時間でしか直らない。"""


class ResearchInputInvalidError(PermanentError):
    """保存された依頼が契約を満たさない・見つからない。同じ入力では必ず同じ結果になる。"""


class ResearchOutputInvalidError(PermanentError):
    """Handler の出力が実行器の契約（依頼 ID・成果物の型・JSON）を満たさない。

    B2 の Handler は決定的（同じ入力で同じ出力）なので permanent。LLM を使う Handler の出力の
    揺れは、その段（ADR-0038 / ADR-0039）で retryable な別の型にする。
    """


def _walk(cls: type[BaseException]) -> list[type[BaseException]]:
    found: list[type[BaseException]] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_walk(sub))
    return found


def _base_failure_class(cls: type[BaseException]) -> FailureClass:
    """MRO の中で最初に現れる ``domain.errors`` の型の失敗クラス（基底の表を引くだけ）。"""
    for klass in cls.__mro__:
        if klass.__module__ == "domain.errors" and klass.__name__ in FAILURE_CLASS_BY_TYPE_NAME:
            return FAILURE_CLASS_BY_TYPE_NAME[klass.__name__]
    return FailureClass.NEEDS_INPUT  # INV-12: 分類できないものは人間へ回す


def _build_research_table() -> dict[str, FailureClass]:
    table: dict[str, FailureClass] = {}
    for cls in _walk(DomainError):
        if cls.__module__ != __name__:
            continue
        if cls.__name__ in FAILURE_CLASS_BY_TYPE_NAME:
            raise RuntimeError(f"research error {cls.__name__} shadows a domain.errors type name")
        table[cls.__name__] = _base_failure_class(cls)
    return table


#: 型名 -> 失敗クラス（research の型だけ）。``classify_failure`` の isinstance 分類と一致する。
RESEARCH_FAILURE_CLASS_BY_TYPE_NAME: dict[str, FailureClass] = _build_research_table()

#: retry しない research の例外の型名。Worker は ``NON_RETRYABLE_ERROR_TYPE_NAMES`` と合わせて渡す。
RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES: tuple[str, ...] = tuple(
    name
    for name, failure_class in RESEARCH_FAILURE_CLASS_BY_TYPE_NAME.items()
    if failure_class in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
)


def research_failure_class_from_type_name(type_name: str | None) -> FailureClass:
    """research の表を引き、無ければ基底の表（未知の名前は ``needs_input``。INV-12）。"""
    if type_name is not None and type_name in RESEARCH_FAILURE_CLASS_BY_TYPE_NAME:
        return RESEARCH_FAILURE_CLASS_BY_TYPE_NAME[type_name]
    return failure_class_from_type_name(type_name)


__all__ = [
    "RESEARCH_FAILURE_CLASS_BY_TYPE_NAME",
    "RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES",
    "ResearchAmbiguousCallError",
    "ResearchArtifactReadbackError",
    "ResearchBudgetExceededError",
    "ResearchIdempotencyConflictError",
    "ResearchInputInvalidError",
    "ResearchOutputInvalidError",
    "ResearchProviderAuthError",
    "ResearchProviderNotConfiguredError",
    "ResearchSourceUnavailableError",
    "research_failure_class_from_type_name",
]
