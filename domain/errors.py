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


class ScriptOutputUnparseableError(RetryableError):
    """生成器の出力が JSON としてパースできない（ADR-0014）。

    LLM は同じ入力でも違う出力を返すので ``permanent`` の条件
    （同じ入力で必ず同じ失敗になる）を満たさない。修復はしない。
    """


class ScriptSchemaViolationError(RetryableError):
    """パースはできたがスキーマ違反（ADR-0014）。"""


class PromptContractError(NeedsInputError):
    """同じ入力で規定ラウンド連続して同種の違反。

    生成揺れではなくプロンプトとスキーマの不整合。人間が直せば回復するので
    ``permanent`` にはしない（ADR-0014）。
    """


class ProviderTimeoutError(RetryableError):
    """外部生成器がタイムアウトした。課金済みかは不明なので予約は未照合のまま。"""


class ProviderInvocationError(RetryableError):
    """外部生成器が非zero終了した。exit code の数値で分岐しない（規約が非公開）。"""


class ProviderUnavailableError(NeedsInputError):
    """CLI不在・未認証・権限拒否。再実行しても同じだが人間が直せば回復する。"""


class UnreconciledReservationError(NeedsInputError):
    """evidence の無い予約が残っている（ADR-0013）。

    呼ばない・消さない・解放しない。人手照合を待つ。
    """


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


def _iter_subclasses(cls: type[BaseException]) -> list[type[BaseException]]:
    found: list[type[BaseException]] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_iter_subclasses(sub))
    return found


def _build_type_name_table() -> dict[str, FailureClass]:
    """型名 -> 失敗クラス。**継承から自動導出する。**

    Temporal の workflow 側は例外オブジェクトではなく型名しか受け取れない。
    ここを手書きの表にすると、新しいサブクラスを足した人が登録を忘れ、
    その失敗は黙って ``needs_input`` に落ちる（安全側だが意図とずれる）。
    定義を2箇所に書かないため、基底クラスの表から派生させる（AGENTS.md §8）。
    """
    table: dict[str, FailureClass] = {}
    for base, failure_class in _FAILURE_CLASS_BY_TYPE.items():
        table[base.__name__] = failure_class
        for sub in _iter_subclasses(base):
            # 多重継承時は最初に見つかった基底が勝つ。isinstance 分類と同じ順序。
            table.setdefault(sub.__name__, failure_class)
    return table


#: 型名 -> 失敗クラス。``classify_failure`` の isinstance 分類と一致する。
FAILURE_CLASS_BY_TYPE_NAME: dict[str, FailureClass] = _build_type_name_table()

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
