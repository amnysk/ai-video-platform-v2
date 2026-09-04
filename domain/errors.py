"""失敗クラスの例外階層（docs/failure-policy.md §1）。

失敗の分類は**例外の型**で行う。メッセージ文字列で判定しない。
"""

from __future__ import annotations

from contracts.states import FailureClass


class DomainError(Exception):
    """このプロジェクトのドメイン例外の基底。"""


class TransientError(DomainError):
    """一時障害。Temporalの自動retryに委ねる。"""


class RetryableError(DomainError):
    """再実行で解決しうる失敗。上限付きでretryする。"""


class NeedsInputError(DomainError):
    """人間の判断が要る失敗。retryせず blocked にする。"""


class PermanentError(DomainError):
    """入力自体が不正。retryしても同じ結果になる。"""


class InvalidTransitionError(DomainError):
    """表に無い状態遷移を永続化しようとした。"""


class ArtifactConflictError(DomainError):
    """同じキーに異なる内容を書こうとした（INV-11違反）。"""


_FAILURE_CLASS_BY_TYPE: dict[type[BaseException], FailureClass] = {
    TransientError: FailureClass.TRANSIENT,
    RetryableError: FailureClass.RETRYABLE,
    NeedsInputError: FailureClass.NEEDS_INPUT,
    PermanentError: FailureClass.PERMANENT,
}

#: 型名 -> 失敗クラス。Temporalのworkflow側は例外オブジェクトではなく型名しか
#: 受け取れないため、同じ表からこちらも導出する（定義を2箇所に書かない）。
FAILURE_CLASS_BY_TYPE_NAME: dict[str, FailureClass] = {
    exc.__name__: cls for exc, cls in _FAILURE_CLASS_BY_TYPE.items()
}

#: retryしない失敗クラスの例外型名。TemporalのRetryPolicyへ渡す。
NON_RETRYABLE_ERROR_TYPE_NAMES: tuple[str, ...] = tuple(
    name
    for name, cls in FAILURE_CLASS_BY_TYPE_NAME.items()
    if cls in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
)


def classify_failure(exc: BaseException) -> FailureClass:
    """例外を失敗クラスへ分類する。

    分類できない例外は ``NEEDS_INPUT``。自動修復に流さず人間へ回す（INV-12）。
    ``PERMANENT`` は「同じ入力で必ず同じ失敗になる」と示せる場合だけなので、
    未知の例外をそこへ落とすことはしない。
    """
    for exc_type, failure_class in _FAILURE_CLASS_BY_TYPE.items():
        if isinstance(exc, exc_type):
            return failure_class
    return FailureClass.NEEDS_INPUT


def failure_class_from_type_name(type_name: str | None) -> FailureClass:
    """例外の型名から失敗クラスを引く。未知の型名は ``NEEDS_INPUT``（INV-12）。"""
    if type_name is None:
        return FailureClass.NEEDS_INPUT
    return FAILURE_CLASS_BY_TYPE_NAME.get(type_name, FailureClass.NEEDS_INPUT)
