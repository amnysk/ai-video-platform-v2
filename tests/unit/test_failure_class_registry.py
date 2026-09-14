"""失敗クラスの型名レジストリ（ADR-0014）。

Temporal は例外オブジェクトではなく**型名**しか workflow へ運ばない
（`workers/dummy/workflows.py` が `ApplicationError.type` で引く）。
そのため新しい例外サブクラスを定義しても型名表に載っていなければ、
`failure_class_from_type_name` は既定の `needs_input` を返す。
安全側ではあるが意図とずれるので、**表は継承から自動導出**されなければならない。
"""

from __future__ import annotations

import pytest

from contracts.states import FailureClass
from domain import errors
from domain.errors import (
    FAILURE_CLASS_BY_TYPE_NAME,
    NON_RETRYABLE_ERROR_TYPE_NAMES,
    DomainError,
    classify_failure,
    failure_class_from_type_name,
)


def _all_domain_error_subclasses() -> set[type[BaseException]]:
    found: set[type[BaseException]] = set()
    for value in vars(errors).values():
        if isinstance(value, type) and issubclass(value, DomainError):
            found.add(value)
    return found


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (errors.ScriptOutputUnparseableError("x"), FailureClass.RETRYABLE),
        (errors.ScriptSchemaViolationError("x"), FailureClass.RETRYABLE),
        (errors.ProviderTimeoutError("x"), FailureClass.RETRYABLE),
        (errors.ProviderInvocationError("x"), FailureClass.RETRYABLE),
        (errors.PromptContractError("x"), FailureClass.NEEDS_INPUT),
        (errors.ProviderUnavailableError("x"), FailureClass.NEEDS_INPUT),
        (errors.UnreconciledReservationError("x"), FailureClass.NEEDS_INPUT),
    ],
)
def test_new_exceptions_classify_by_their_base(exc: Exception, expected: FailureClass) -> None:
    assert classify_failure(exc) is expected


def test_every_domain_exception_is_reachable_by_type_name() -> None:
    """継承から自動導出されること。手で表に足す運用にしない（AGENTS.md §8）。"""
    classifiable = {
        cls
        for cls in _all_domain_error_subclasses()
        if classify_failure(cls("probe")) is not None
        and cls.__name__ not in {"DomainError", "InvalidTransitionError", "ArtifactConflictError"}
    }
    missing = sorted(
        cls.__name__ for cls in classifiable if cls.__name__ not in FAILURE_CLASS_BY_TYPE_NAME
    )
    assert not missing, (
        f"型名表に載っていない例外: {missing}. Temporal は型名しか運ばないので、"
        "これらは workflow 側で needs_input に落ちる"
    )


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("ScriptOutputUnparseableError", FailureClass.RETRYABLE),
        ("ScriptSchemaViolationError", FailureClass.RETRYABLE),
        ("ProviderTimeoutError", FailureClass.RETRYABLE),
        ("PromptContractError", FailureClass.NEEDS_INPUT),
        ("ProviderUnavailableError", FailureClass.NEEDS_INPUT),
        ("UnreconciledReservationError", FailureClass.NEEDS_INPUT),
    ],
)
def test_type_name_lookup_matches_isinstance_classification(
    type_name: str, expected: FailureClass
) -> None:
    assert failure_class_from_type_name(type_name) is expected


def test_needs_input_subclasses_are_not_retried_by_temporal() -> None:
    """人間待ちの失敗を Temporal が自動retryしない（課金の無駄と blocked の遅延を防ぐ）。"""
    for name in ("PromptContractError", "ProviderUnavailableError", "UnreconciledReservationError"):
        assert name in NON_RETRYABLE_ERROR_TYPE_NAMES


def test_retryable_subclasses_are_not_marked_non_retryable() -> None:
    for name in ("ScriptOutputUnparseableError", "ProviderTimeoutError"):
        assert name not in NON_RETRYABLE_ERROR_TYPE_NAMES


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (errors.StoryboardOutputUnparseableError("x"), FailureClass.RETRYABLE),
        (errors.StoryboardSchemaViolationError("x"), FailureClass.RETRYABLE),
        (errors.WorkspaceUnavailableError("x"), FailureClass.RETRYABLE),
        (errors.StoryboardInputMissingError("x"), FailureClass.NEEDS_INPUT),
        (errors.StoryboardInputInvalidError("x"), FailureClass.NEEDS_INPUT),
        (errors.GenerationSpecUnavailableError("x"), FailureClass.NEEDS_INPUT),
    ],
)
def test_storyboard_exceptions_classify_by_their_base(
    exc: Exception, expected: FailureClass
) -> None:
    """ADR-0015 の失敗クラス。型名経由（Temporal）と isinstance 分類が一致すること。"""
    assert classify_failure(exc) is expected
    assert failure_class_from_type_name(type(exc).__name__) is expected
    non_retryable = expected in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
    assert (type(exc).__name__ in NON_RETRYABLE_ERROR_TYPE_NAMES) is non_retryable


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (errors.ProviderSubmitAmbiguousError("x"), FailureClass.NEEDS_INPUT),
        (errors.ProviderRejectedError("x"), FailureClass.NEEDS_INPUT),
        (errors.ProviderJobFailedError("x"), FailureClass.RETRYABLE),
        (errors.ProviderPollDeadlineError("x"), FailureClass.RETRYABLE),
        (errors.MediaValidationError("x"), FailureClass.RETRYABLE),
        (errors.ProductionInputMissingError("x"), FailureClass.NEEDS_INPUT),
        (errors.ProductionInputInvalidError("x"), FailureClass.NEEDS_INPUT),
        (errors.VoiceLanguageUnsupportedError("x"), FailureClass.NEEDS_INPUT),
    ],
)
def test_production_exceptions_classify_by_their_base(
    exc: Exception, expected: FailureClass
) -> None:
    """ADR-0017 の失敗クラス。型名経由（Temporal）と isinstance 分類が一致すること。"""
    assert classify_failure(exc) is expected
    assert failure_class_from_type_name(type(exc).__name__) is expected
    non_retryable = expected in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
    assert (type(exc).__name__ in NON_RETRYABLE_ERROR_TYPE_NAMES) is non_retryable
