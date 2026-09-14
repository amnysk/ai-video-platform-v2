"""Activity 境界での例外の写像（docs/failure-policy.md / ADR-0017 §7）。

- ドメイン例外 → ``ApplicationError(type=<型名>, non_retryable=<needs_input|permanent>)``
- DB 接続断・MinIO 通信失敗・作業領域の OSError → ``TransientError``（Temporal が retry）
- 未分類の例外はそのまま（Temporal の retry と workflow の型名分類 / INV-12）
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from temporalio.exceptions import ApplicationError
from urllib3.exceptions import MaxRetryError, ProtocolError

from contracts.states import FailureClass
from domain import errors
from domain.errors import failure_class_from_type_name
from infrastructure.production.activity_errors import raise_activity_error, translate_error


def _raised(exc: Exception, **kw) -> BaseException:
    try:
        raise_activity_error(exc, **kw)
    except BaseException as raised:  # noqa: BLE001
        return raised
    raise AssertionError("raise_activity_error returned")


@pytest.mark.parametrize(
    ("exc", "non_retryable"),
    [
        (errors.ProviderSubmitAmbiguousError("x"), True),
        (errors.UnreconciledReservationError("x"), True),
        (errors.ProviderRejectedError("x"), True),
        (errors.ProductionInputInvalidError("x"), True),
        (errors.InvalidTransitionError("x"), True),
        (errors.ArtifactConflictError("x"), True),
        (errors.TransientError("x"), False),
        (errors.ProviderJobFailedError("x"), False),
        (errors.MediaValidationError("x"), False),
        (errors.ProviderPollDeadlineError("x"), False),
    ],
)
def test_domain_errors_become_typed_application_errors(exc, non_retryable) -> None:
    raised = _raised(exc)
    assert isinstance(raised, ApplicationError)
    assert raised.type == type(exc).__name__
    assert raised.non_retryable is non_retryable
    assert raised.__cause__ is exc


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError("SELECT 1", {}, Exception("server closed the connection")),
        InterfaceError("SELECT 1", {}, Exception("connection already closed")),
        DBAPIError("SELECT 1", {}, Exception("reset"), connection_invalidated=True),
        MaxRetryError(pool=None, url="/artifacts/x"),  # type: ignore[arg-type]
        ProtocolError("Connection aborted."),
        ConnectionResetError("reset by peer"),
        OSError(28, "No space left on device"),
    ],
)
def test_infrastructure_outages_are_transient(exc) -> None:
    translated = translate_error(exc)
    assert isinstance(translated, errors.TransientError)
    raised = _raised(exc)
    assert isinstance(raised, ApplicationError)
    assert raised.type == "TransientError" and raised.non_retryable is False


def test_non_disconnect_dbapi_error_is_not_transient() -> None:
    exc = DBAPIError("INSERT", {}, Exception("unique violation"), connection_invalidated=False)
    assert translate_error(exc) is exc


def test_unclassified_errors_pass_through_unchanged() -> None:
    exc = RuntimeError("bug")
    assert _raised(exc) is exc


def test_round_consumed_is_final_for_the_activity_but_retryable_for_the_workflow() -> None:
    """await で「このラウンドは消費済み」: Temporal は retry しない / workflow は新ラウンド。"""
    exc = errors.ProviderJobFailedError("round consumed")
    raised = _raised(exc, final_for_activity=(errors.ProviderJobFailedError,))
    assert isinstance(raised, ApplicationError)
    assert raised.non_retryable is True
    assert failure_class_from_type_name(raised.type) is FailureClass.RETRYABLE
