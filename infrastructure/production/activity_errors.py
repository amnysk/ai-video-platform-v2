"""Activity 境界での例外の写像（docs/failure-policy.md §1 / ADR-0017 §7）。

production の3 worker（画像・音声・動画）が共有する。
worker 間 import を避けるため infrastructure に置く（INV-3）。

1. ``translate_error``: インフラの一時障害をドメインの ``TransientError`` にする

   - DB の接続断・操作エラー（SQLAlchemy ``OperationalError`` / ``InterfaceError`` /
     ``connection_invalidated`` な ``DBAPIError``、psycopg の同名例外）
   - オブジェクトストア（MinIO / urllib3）の通信失敗・5xx（``S3Error`` は 5xx 応答と
     ``S3_TRANSIENT_CODES`` だけ。``NoSuchKey`` などの 4xx はそのまま返す）
   - 作業領域などの ``OSError``

   それ以外はそのまま返す（一意制約違反などは一時障害ではない）。

2. ``raise_activity_error``: ドメイン例外を ``ApplicationError(type=<型名>)`` にして送出する。
   ``non_retryable`` は失敗クラスが needs_input / permanent のとき。workflow は型名で分類する。
   ``InvalidTransitionError`` / ``ArtifactConflictError`` は分類上 needs_input（食い違いの兆候）。
   未分類の例外はそのまま送出する（Temporal の retry と workflow の型名分類 / INV-12）。
   ``details`` は ApplicationError の details へそのまま載せる（render は job id を載せる）。

   ``final_for_activity`` に挙げた型は、失敗クラスは変えずに **Activity の retry だけ止める**。
   await Activity で「このラウンドは消費済み」（``ProviderJobFailedError`` など）は、同じ
   Activity を retry しても同じ結果にしかならない。workflow は型名から retryable と分類して
   新しいラウンドへ進む（ADR-0017 §4）。
"""

from __future__ import annotations

from typing import NoReturn

import psycopg
from minio.error import S3Error
from minio.error import ServerError as MinioServerError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from temporalio.exceptions import ApplicationError
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from contracts.states import FailureClass
from domain.errors import (
    DomainError,
    MediaValidationError,
    ProviderJobFailedError,
    TransientError,
    classify_failure,
)

#: S3 のエラーコードのうち、サーバ側の一時障害を示すもの（HTTP 5xx / 流量制限）。
S3_TRANSIENT_CODES: frozenset[str] = frozenset(
    {"InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout", "ServiceFailure"}
)

_NON_RETRYABLE_CLASSES = frozenset({FailureClass.NEEDS_INPUT, FailureClass.PERMANENT})

#: await Activity で「ラウンドは確定済み」を示す型。retry しても台帳から同じ結果が返るだけ。
#: - ``ProviderJobFailedError``: provider のジョブ失敗 / 取得物なしで spent / 閉じた予約
#: - ``MediaValidationError``: spent 済みの取得物が規則を満たさない（再検証しても同じバイト列）
AWAIT_ROUND_FINAL_ERRORS: tuple[type[DomainError], ...] = (
    ProviderJobFailedError,
    MediaValidationError,
)


def _summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _s3_is_transient(exc: S3Error) -> bool:
    """5xx 応答か一時障害のコード。NoSuchKey 等（4xx）は一時障害ではない（呼び出し側が扱う）。"""
    if exc.code in S3_TRANSIENT_CODES:
        return True
    status = getattr(getattr(exc, "response", None), "status", None)
    return isinstance(status, int) and status >= 500


def translate_error(exc: BaseException) -> BaseException:
    """インフラの一時障害を ``TransientError`` に写す。該当しなければ ``exc`` をそのまま返す。"""
    if isinstance(exc, DomainError):
        return exc
    if isinstance(exc, (OperationalError, InterfaceError)) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    ):
        return TransientError(f"database unavailable: {_summary(exc)}")
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return TransientError(f"database unavailable: {_summary(exc)}")
    if isinstance(exc, S3Error) and _s3_is_transient(exc):
        return TransientError(f"object store unavailable: {_summary(exc)}")
    if isinstance(exc, (Urllib3HTTPError, MinioServerError)):
        return TransientError(f"object store unavailable: {_summary(exc)}")
    if isinstance(exc, OSError):
        return TransientError(f"local I/O failed: {_summary(exc)}")
    return exc


def raise_activity_error(
    exc: BaseException,
    *,
    final_for_activity: tuple[type[BaseException], ...] = (),
    details: tuple[object, ...] = (),
) -> NoReturn:
    """``except Exception as exc:`` の中から呼ぶ。必ず送出する。"""
    translated = translate_error(exc)
    if isinstance(translated, DomainError):
        name = type(translated).__name__
        non_retryable = classify_failure(translated) in _NON_RETRYABLE_CLASSES or isinstance(
            translated, final_for_activity
        )
        raise ApplicationError(
            f"{name}: {translated}", *details, type=name, non_retryable=non_retryable
        ) from exc
    raise exc


__all__ = ["AWAIT_ROUND_FINAL_ERRORS", "raise_activity_error", "translate_error"]
